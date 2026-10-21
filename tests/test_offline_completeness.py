import unittest

from test_reader_resources import ReaderFixture
from src.services.archive.offline_completeness import offline_cache_state
from src.services.archive.reader_resources import write_json
from src.services.archive.resource_state_service import audit_article, set_attempt


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


if __name__ == "__main__":
    unittest.main()
