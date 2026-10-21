import unittest
from pathlib import Path
from unittest.mock import Mock

from test_reader_resources import ReaderFixture
from src.services.archive.offline_completeness import offline_cache_state
from src.services.archive.reader_resources import write_json
from src.services.archive.resource_state_service import audit_article, set_attempt
from src.services.archive.offline_cache_job_service import OfflineCacheJobService


class OfflineCompletenessTests(ReaderFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        (self.article / "assets").mkdir()
        (self.article / "assets/image.png").write_bytes(self.blob)
        (self.article / "index.html").write_text(
            '<html><div id="js_content"><p>正文</p><img src="assets/image.png"></div></html>',
            encoding="utf-8",
        )
        self.manifest = {
            "version": 1,
            "files": ["assets/image.png"],
            "required_images_complete": True,
            "warnings": [],
        }

    def test_browser_response_warnings_do_not_override_actual_local_resources(self):
        self.manifest["warnings"] = [
            "读取页面响应失败：https://mp.weixin.qq.com/mp/jsmonitor"
        ]
        status, done, total, reason = offline_cache_state(self.article, self.manifest)
        self.assertEqual((status, done, total), ("complete", 1, 1))
        self.assertIn("非关键", reason)

    def test_missing_and_corrupt_local_files_are_incomplete(self):
        image = self.article / "assets/image.png"
        image.write_bytes(b"<html>not an image</html>")
        self.assertEqual(
            offline_cache_state(self.article, self.manifest)[:3], ("partial", 0, 1)
        )
        image.unlink()
        self.assertEqual(
            offline_cache_state(self.article, self.manifest)[:3], ("partial", 0, 1)
        )
        (self.article / "index.html").write_text("", encoding="utf-8")
        self.assertEqual(offline_cache_state(self.article, self.manifest)[0], "partial")

    def test_uncaptured_remote_image_is_in_the_expected_count(self):
        with (self.article / "index.html").open("a", encoding="utf-8") as stream:
            stream.write('<img src="https://images.example/missing">')
        self.assertEqual(
            offline_cache_state(self.article, self.manifest)[:3], ("partial", 1, 2)
        )

    def test_existing_page_can_be_verified_without_a_generation_manifest(self):
        self.assertEqual(offline_cache_state(self.article, {})[:3], ("complete", 1, 1))

    def test_unfinished_loading_is_not_complete_even_when_saved_files_exist(self):
        self.manifest["warnings"] = ["已达到最长滚动加载时间"]
        status, _, _, reason = offline_cache_state(self.article, self.manifest)
        self.assertEqual(status, "partial")
        self.assertIn("滚动加载未结束", reason)

    def test_only_the_obsolete_warning_verdict_is_reassessed(self):
        write_json(self.article / "assets/awa-manifest.json", self.manifest)
        key, _ = audit_article(self.db, self.storage, 1)
        set_attempt(
            self.db, key, ["offline"], "partial", "原离线资源存在缺失或下载警告"
        )
        _, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["offline"]["attempt_status"], "complete")
        set_attempt(self.db, key, ["offline"], "failed", "本次网络请求超时")
        _, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["offline"]["artifact_status"], "complete")
        self.assertEqual(state["offline"]["attempt_status"], "failed")


class OfflineJobCompletenessTests(ReaderFixture, unittest.TestCase):
    def run_cache(self, *, missing_image=False):
        control = Mock()

        def start(**kwargs):
            stage = Path(kwargs["payload"]["stage_dir"])
            (stage / "assets").mkdir(parents=True)
            (stage / "assets/image.png").write_bytes(self.blob)
            source = (
                "https://images.example/missing"
                if missing_image
                else "assets/image.png"
            )
            (stage / "index.html").write_text(
                f'<div id="js_content"><p>正文</p><img src="{source}"></div>',
                encoding="utf-8",
            )
            write_json(
                stage / "assets/awa-manifest.json",
                {
                    "files": ["assets/image.png"],
                    "warnings": [
                        "读取页面响应失败：https://mp.weixin.qq.com/mp/jsmonitor"
                    ],
                },
            )
            attempt = Mock()
            attempt.wait_result.return_value = {"ok": True, "resource_count": 1}
            return attempt

        control.start.side_effect = start
        service = OfflineCacheJobService(
            database_path=self.db,
            storage_root=self.storage,
            temp_root=self.root / "tmp",
            browser_cache_dir=self.root / "browser",
            max_concurrent_processes=1,
            max_scroll_seconds=0,
            resource_timeout_seconds=1,
            process_control=control,
        )
        job = service.create_articles_job([1], start=False)
        service.run_job(job.job_id)
        return service.get_job(job.job_id).to_payload()

    def test_nonessential_warning_finishes_successfully(self):
        result = self.run_cache()
        self.assertEqual((result["status"], result["failed"]), ("done", 0))
        _, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["offline"]["artifact_status"], "complete")

    def test_incomplete_first_cache_counts_as_failure(self):
        result = self.run_cache(missing_image=True)
        self.assertEqual((result["status"], result["failed"]), ("partial_failed", 1))
        _, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["offline"]["artifact_status"], "partial")
        self.assertEqual(
            (state["offline"]["completed"], state["offline"]["expected"]), (1, 2)
        )

    def test_incomplete_refresh_keeps_the_complete_page(self):
        self.run_cache()
        before = (self.article / "index.html").read_bytes()
        result = self.run_cache(missing_image=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual((self.article / "index.html").read_bytes(), before)
        _, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["offline"]["artifact_status"], "complete")
        self.assertEqual(state["offline"]["attempt_status"], "failed")


if __name__ == "__main__":
    unittest.main()
