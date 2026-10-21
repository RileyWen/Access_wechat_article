from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from test_reader_resources import ReaderFixture
from src.config.config_loader import load_app_config
from dev_server import DevBackendContext, create_backend_app


class ReaderApiTests(ReaderFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        project = Path(__file__).resolve().parents[1]
        from dataclasses import replace

        config = load_app_config(project / "data/custom.yaml", project_root=project)
        config = replace(
            config, storage=replace(config.storage, article_storage_root=self.storage)
        )
        self.flow = Mock()
        self.backend = DevBackendContext(
            project_root=project,
            runtime=SimpleNamespace(config=config),
            db_path=self.db,
            task_manager=Mock(),
            main_flow_service=self.flow,
        )
        self.client = TestClient(create_backend_app(self.backend))
        self.addCleanup(self.client.close)

    def test_article_list_reports_per_resource_counts_and_filters(self):
        response = self.client.get("/api/archive/accounts/1/articles")
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["resources"]["avatars"]["expected"], 1)
        self.assertEqual(item["resources"]["avatars"]["completed"], 0)
        self.assertFalse(item["readerAvailable"])
        response = self.client.get(
            "/api/archive/accounts/1/articles?resourceStatus=complete&resourceStages=avatars"
        )
        self.assertEqual(response.json()["total"], 0)
        self.service.repair(1)
        response = self.client.get(
            "/api/archive/accounts/1/articles?resourceStatus=complete&resourceStages=avatars,reader"
        )
        self.assertEqual(response.json()["total"], 1)

    def test_retry_validates_selection_and_passes_exact_resources_to_main_flow(self):
        for payload in [
            {"articleIds": []},
            {"articleIds": [999]},
            {"articleIds": [1], "resources": ["bogus"]},
        ]:
            self.assertEqual(
                self.client.post(
                    "/api/archive/articles/retry", json=payload
                ).status_code,
                400,
            )
        with patch(
            "dev_server._main_flow_snapshot_payload",
            return_value={"ok": True, "status": "running"},
        ):
            response = self.client.post(
                "/api/archive/articles/retry",
                json={
                    "articleIds": [1],
                    "resources": ["avatars", "reader"],
                    "mode": "repair",
                },
            )
        self.assertEqual(response.status_code, 200)
        command = self.flow.start.call_args.args[0]
        self.assertEqual(command.retry_article_ids, (1,))
        self.assertEqual(command.retry_resources, ("avatars", "reader"))
        self.assertTrue(command.skip_collected_records)

    def test_open_reader_rejects_missing_page_then_opens_only_local_file(self):
        self.assertEqual(
            self.client.post(
                "/api/archive/articles/open-reader", json={"articleId": 1}
            ).status_code,
            404,
        )
        self.service.repair(1)
        with patch("webbrowser.open", return_value=True) as opened:
            response = self.client.post(
                "/api/archive/articles/open-reader", json={"articleId": 1}
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(opened.call_args.args[0].startswith("file:"))


if __name__ == "__main__":
    unittest.main()
