from datetime import datetime
import json
from threading import Event
import unittest
from unittest.mock import patch

from test_reader_resources import PACKAGE, ReaderFixture
from src.domain.models import ResourceManifest, TaskContext
from src.domain.enums import TaskStatus
from src.modules.request.comment_requester import CommentFetchData
from src.services.capture.comment_collect_service import CommentCollectService
from src.services.capture.html_parse_save_service import ArticleSaveData
from src.services.archive.resource_state_service import audit_article
from src.storage.repositories.article_repository import ArticleRepository
from src.storage.sqlite.connection import sqlite_connection


class CommentCommitTests(ReaderFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.context = TaskContext(
            task_id="test",
            proxy_lease_id="test",
            db_path=self.db,
            storage_root=self.storage,
            temp_dir=self.root / "tmp",
            started_at=datetime.now(),
            cancel_token=Event(),
        )
        self.saved = ArticleSaveData(
            article_id=1,
            account_id=1,
            history_id=1,
            article_directory=self.article,
            archive_dir="article",
            detail_path=self.article / "article_detail.json",
            resource_manifest=ResourceManifest(),
            html_source="test",
            attempt_id="attempt",
        )
        with sqlite_connection(self.db, write=False) as conn:
            self.record = ArticleRepository(conn).get_by_id(1)
        self.collector = CommentCollectService()

    def store(self, package):
        fetched = CommentFetchData(
            package=package,
            comment_count=len(package["comments"]),
            reply_count=1,
            page_count=1,
            pagination_complete=package["summary"]["pagination_complete"],
            stop_reason=package["summary"]["stop_reason"],
        )
        with (
            patch(
                "src.services.capture.comment_collect_service.external_proxy",
                return_value="",
            ),
            patch(
                "src.services.archive.reader_resources.ImageDownloader._fetch",
                return_value=self.blob,
            ),
        ):
            return self.collector._store_fetched_comments(
                context=self.context,
                article=self.saved,
                record=self.record,
                started=datetime.now(),
                started_monotonic=self.collector._monotonic(),
                fetched=fetched,
                html_comment_count=2,
                article_url="https://mp.weixin.qq.com/s/abc",
                headers={},
                timeout_seconds=10,
                on_event=None,
            )

    def test_success_replaces_old_comment_assets_as_one_bundle(self):
        stale = self.article / "comments/assets/stale.jpg"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"old")
        result = self.store(PACKAGE)
        self.assertEqual(result.status, TaskStatus.SUCCESS, result.message)
        self.assertFalse(stale.exists())
        self.assertEqual(result.data.asset_count, 1)
        _, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["avatars"]["artifact_status"], "complete")

    def test_partial_comments_do_not_replace_previous_data(self):
        before = (self.article / "comments/final.json").read_bytes()
        package = json.loads(json.dumps(PACKAGE))
        package["summary"]["pagination_complete"] = False
        package["summary"]["stop_reason"] = "max_pages_reached"
        result = self.store(package)
        self.assertEqual(result.status, TaskStatus.FAILED, result.message)
        self.assertEqual((self.article / "comments/final.json").read_bytes(), before)
        _, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["comments"]["artifact_status"], "complete")
        self.assertEqual(state["comments"]["attempt_status"], "failed")

    def test_unknown_comment_count_does_not_delete_previous_data(self):
        before = (self.article / "comments/final.json").read_bytes()
        result = self.collector._finish_without_comment_resources(
            context=self.context,
            article=self.saved,
            record=self.record,
            started=datetime.now(),
            started_monotonic=self.collector._monotonic(),
            html_comment_count=None,
            stop_reason="html_comment_count_missing",
            on_event=None,
        )
        self.assertEqual(result.status, TaskStatus.FAILED, result.message)
        self.assertEqual((self.article / "comments/final.json").read_bytes(), before)

    def test_cancel_preserves_previous_comments(self):
        before = (self.article / "comments/final.json").read_bytes()
        self.context.cancel_token.set()
        result = self.store(PACKAGE)
        self.assertEqual(result.status, TaskStatus.FAILED, result.message)
        self.assertEqual((self.article / "comments/final.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
