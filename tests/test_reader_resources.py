from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from PIL import Image
from lxml import html

from src.services.archive.reader_resources import (
    ImageDownloader,
    comments_completeness,
    write_json,
)
from src.services.archive.resource_state_service import (
    audit_article,
    needed,
    set_attempt,
)
from src.services.archive.reader_service import ReaderService


SOURCE = '<html><body><div id="js_content"><p>正文</p><table><tr><td>数据</td></tr></table><img data-src="https://images.example/body"><script>fetch("https://bad.example")</script></div></body></html>'
PACKAGE = {
    "summary": {"pagination_complete": True, "stop_reason": "continue_flag_false"},
    "comments": [
        {
            "id": "c1",
            "nick_name": "读者<script>x</script>",
            "content": "你好 <img src=x>",
            "logo_url": "https://images.example/avatar",
            "reply_new": {
                "reply_total_cnt": 1,
                "reply_list": [
                    {
                        "reply_id": "r1",
                        "content": "回复",
                        "logo_url": "https://images.example/avatar",
                    }
                ],
            },
        }
    ],
}


class ReaderFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = self.root / "storages"
        self.article = self.storage / "article"
        (self.article / "origin").mkdir(parents=True)
        (self.article / "origin/main.html").write_text(SOURCE, encoding="utf-8")
        write_json(
            self.article / "article_detail.json",
            {
                "article_title": "标题",
                "account_name": "测试",
                "article_short_link": "https://mp.weixin.qq.com/s/abc",
            },
        )
        write_json(self.article / "comments/final.json", PACKAGE)
        self.db = self.root / "test.sqlite"
        sql = (
            Path(__file__).resolve().parents[1]
            / "data/sql/create_script/create_awa_v2_1.sql"
        ).read_text()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(sql)
            conn.execute("INSERT INTO awa_public_accounts(account_name) VALUES('测试')")
            conn.execute(
                "INSERT INTO awa_public_articles(account_id,article_title,article_link,archive_dir) VALUES(1,'标题','https://mp.weixin.qq.com/s/abc','article')"
            )
            conn.commit()
        image = BytesIO()
        Image.new("RGB", (4, 4), "red").save(image, format="PNG")
        self.blob = image.getvalue()
        self.download = ImageDownloader()
        self.download._fetch = lambda url: self.blob
        self.service = ReaderService(
            database_path=self.db,
            storage_root=self.storage,
            temp_root=self.root / "tmp",
            downloader=self.download,
        )


class ReaderTests(ReaderFixture, unittest.TestCase):
    def test_full_render_is_local_portable_and_deduplicated(self):
        result = self.service.repair(1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            len(list((self.article / "comments/assets/avatars").glob("*"))), 1
        )
        reader = self.article / "reader/index.html"
        tree = html.fromstring(reader.read_text())
        self.assertFalse(tree.xpath("//script"))
        self.assertIn("正文", tree.text_content())
        self.assertIn("回复", tree.text_content())
        self.assertEqual(len(tree.xpath('//section[@class="comments"]//img')), 2)
        for src in tree.xpath("//@src"):
            self.assertNotIn("https:", src)
            self.assertTrue((reader.parent / src).is_file())
        import shutil

        moved = self.root / "moved"
        shutil.copytree(self.article, moved)
        for src in tree.xpath("//@src"):
            self.assertTrue((moved / "reader" / src).is_file())
        self.assertTrue(Path(str(self.db) + ".before-reader-v1.bak").is_file())

    def test_partial_replies_are_not_complete(self):
        package = json.loads(json.dumps(PACKAGE))
        package["comments"][0]["reply_new"]["reply_total_cnt"] = 2
        self.assertEqual(comments_completeness(package)[0], "partial")
        package["comments"][0]["reply_new"]["reply_total_cnt"] = 1
        package["summary"]["stop_reason"] = "no_new_comments"
        self.assertEqual(comments_completeness(package)[0], "partial")

    def test_failed_refresh_preserves_previous_page_and_exposes_attempt(self):
        self.service.repair(1)
        old = (self.article / "reader/index.html").read_bytes()
        self.download._fetch = lambda url: (_ for _ in ()).throw(ValueError("无法下载"))
        result = self.service.repair(1, ["avatars"], refresh=True)
        # Local repair may publish a partial first version, but must not replace a good one.
        self.assertEqual(result["status"], "partial")
        self.assertEqual((self.article / "reader/index.html").read_bytes(), old)

    def test_reading_page_becomes_stale_when_comments_change(self):
        self.service.repair(1)
        package = json.loads(json.dumps(PACKAGE))
        package["comments"][0]["content"] = "更新内容"
        write_json(self.article / "comments/final.json", package)
        key, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["reader"]["artifact_status"], "partial")
        set_attempt(self.db, key, ["comments"], "failed", "token失效")
        _, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["comments"]["artifact_status"], "complete")
        self.assertIn("comments", needed(state, ["comments"]))

    def test_http_200_html_is_not_saved_as_an_avatar(self):
        self.download._fetch = lambda url: b"<html>login</html>"
        result = self.download.download(
            ["https://images.example/login"],
            self.root / "stage",
            prefix="comments/assets/avatars",
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed"], 0)

    def test_database_failure_rolls_back_replaced_resources(self):
        self.service.repair(1)
        before = {
            str(p.relative_to(self.article)): p.read_bytes()
            for p in self.article.rglob("*")
            if p.is_file()
        }
        from unittest.mock import patch

        with patch(
            "src.storage.repositories.article_repository.ArticleRepository.update_resource_manifest",
            side_effect=RuntimeError("database failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "database failure"):
                self.service.repair(1, ["avatars"], refresh=True)
        after = {
            str(p.relative_to(self.article)): p.read_bytes()
            for p in self.article.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
