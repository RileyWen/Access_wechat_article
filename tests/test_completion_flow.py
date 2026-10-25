from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
import shutil
import json

import unittest
from test_reader_resources import ReaderFixture
from src.services.archive.resource_state_service import (
    audit_article,
    set_attempt,
    pending_targets,
    recover_interrupted,
)
from src.services.main_flow.completion_planner import find_article
from src.services.main_flow.main_flow_coordinator import MainFlowCoordinator
from src.services.main_flow.main_flow_models import (
    MainFlowCommand,
    MainFlowContext,
    HomeArticleTarget,
    SingleArticleReceipt,
)
from src.services.main_flow.main_flow_state import MainFlowState
from src.storage.sqlite.connection import sqlite_connection


class CompletionFlowTests(ReaderFixture, unittest.TestCase):
    def context(self):
        return MainFlowContext(
            task_id="test",
            db_path=self.db,
            storage_root=self.storage,
            temp_dir=self.root / "tmp",
            config_snapshot=SimpleNamespace(),
            cancel_token=Event(),
            started_at=datetime.now(),
            state=MainFlowState(task_id="test", target_count=0),
        )

    def target(self, title="标题"):
        return HomeArticleTarget(
            sequence=1,
            account_name="测试",
            article_date="",
            title_raw=title,
            title_display=title,
            card_rect=(0, 0, 100, 100),
            visible_rect=(0, 0, 100, 100),
            click_point=(20, 20),
            home_window_id=1,
        )

    def add_article(self, number):
        shutil.copytree(self.article, self.storage / f"article{number}")
        detail_path = self.storage / f"article{number}" / "article_detail.json"
        detail = json.loads(detail_path.read_text())
        detail.update(
            article_title=f"标题{number}",
            article_short_link=f"https://mp.weixin.qq.com/s/{number}",
        )
        detail_path.write_text(json.dumps(detail), encoding="utf-8")
        with sqlite_connection(self.db) as conn:
            conn.execute(
                "INSERT INTO awa_public_articles(account_id,article_title,article_link,archive_dir) VALUES(1,?,?,?)",
                (
                    f"标题{number}",
                    f"https://mp.weixin.qq.com/s/{number}",
                    f"article{number}",
                ),
            )

    def test_two_local_repairs_do_not_open_wechat_or_touch_third_article(self):
        self.add_article(2)
        self.add_article(3)
        context = self.context()
        scanner = Mock()
        dispatcher = Mock()
        command = MainFlowCommand(
            retry_article_ids=(1, 2),
            retry_resources=("avatars", "body_images", "reader"),
            skip_collected_records=True,
        )
        with patch(
            "src.services.archive.reader_resources.ImageDownloader._fetch",
            return_value=self.blob,
        ):
            navigation_factory = Mock()
            MainFlowCoordinator(scanner=scanner, dispatcher=dispatcher, navigation_factory=navigation_factory).run(
                context, command
            )
            navigation_factory.assert_not_called()
        scanner.initialize.assert_not_called()
        dispatcher.submit.assert_not_called()
        self.assertTrue((self.storage / "article2/reader/index.html").is_file())
        self.assertFalse((self.storage / "article3/reader").exists())
        self.assertEqual(context.state.snapshot()["errorCount"], 0)

    def test_complete_selected_resources_are_skipped_before_click(self):
        self.service.repair(1)
        target = self.target()
        context = self.context()
        scanner = Mock()
        dispatcher = Mock()
        scanner.initialize.return_value = SimpleNamespace(account_name="测试")
        scanner.scan_visible_targets.return_value = SimpleNamespace(
            targets=(target,), boundary_reached=True
        )
        command = MainFlowCommand(
            build_reader=True, collect_comments=True, skip_collected_records=True
        )
        MainFlowCoordinator(scanner=scanner, dispatcher=dispatcher).run(
            context, command
        )
        dispatcher.submit.assert_not_called()
        self.assertEqual(context.state.snapshot()["skippedCount"], 1)

    def test_complete_retry_is_counted_as_skipped_without_local_work(self):
        self.service.repair(1)
        context = self.context()
        scanner = Mock()
        dispatcher = Mock()
        with patch.object(MainFlowCoordinator, "_run_local") as repair:
            MainFlowCoordinator(scanner=scanner, dispatcher=dispatcher).run(
                context,
                MainFlowCommand(
                    retry_article_ids=(1,),
                    retry_resources=("avatars", "reader"),
                    skip_collected_records=True,
                ),
            )
        repair.assert_not_called()
        scanner.initialize.assert_not_called()
        dispatcher.submit.assert_not_called()
        self.assertEqual(context.state.snapshot()["skippedCount"], 1)

    def test_selected_comment_retry_only_clicks_selected_targets(self):
        self.add_article(2)
        self.add_article(3)
        for article_id in (1, 2):
            key, _ = audit_article(self.db, self.storage, article_id)
            set_attempt(self.db, key, ["comments"], "failed", "旧 token 失效")
        context = self.context()
        scanner = Mock()
        dispatcher = Mock()
        scanner.initialize.return_value = SimpleNamespace(account_name="测试")
        scanner.scan_visible_targets.return_value = SimpleNamespace(
            targets=(self.target(), self.target("标题2"), self.target("标题3")),
            boundary_reached=True,
        )

        def submit(target, options, ctx):
            return SimpleNamespace(
                handle_id=options.target_key, target=target, options=options
            )

        dispatcher.submit.side_effect = submit

        def receipt(handle, **kwargs):
            return SingleArticleReceipt(
                task_id=handle.handle_id,
                target_fingerprint=handle.target.fingerprint,
                status="failed",
                foreground_done=True,
                tab_closed=True,
                message="模拟微信 token 过期",
            )

        dispatcher.wait_foreground_done.side_effect = receipt
        dispatcher.wait_finished.side_effect = receipt
        dispatcher.poll_updates.return_value = []
        command = MainFlowCommand(
            retry_article_ids=(1, 2),
            retry_resources=("comments",),
            skip_collected_records=True,
        )
        MainFlowCoordinator(scanner=scanner, dispatcher=dispatcher).run(
            context, command
        )
        self.assertEqual(
            [c.args[0].title_raw for c in dispatcher.submit.call_args_list],
            ["标题", "标题2"],
        )
        self.assertTrue(
            all(
                not c.args[1].skip_collected_records
                for c in dispatcher.submit.call_args_list
            )
        )

    def test_ambiguous_match_is_not_chosen(self):
        with sqlite_connection(self.db) as conn:
            conn.execute(
                "INSERT INTO awa_public_articles(account_id,article_title,article_link,archive_dir) VALUES(1,'标题','https://mp.weixin.qq.com/s/different','other')"
            )
        with self.assertRaisesRegex(ValueError, "不唯一"):
            find_article(self.db, "测试", "标题")

    def test_interrupted_stage_remains_retryable_after_restart(self):
        key, _ = audit_article(self.db, self.storage, 1)
        set_attempt(self.db, key, ["avatars"], "running")
        recover_interrupted(self.db)
        _, state = audit_article(self.db, self.storage, 1)
        self.assertEqual(state["avatars"]["attempt_status"], "pending")

    def test_failed_first_capture_remains_visible_without_an_article_index(self):
        from src.services.main_flow.article_dispatcher import ArticleDispatcher

        target = self.target("尚未采到的文章")
        context = self.context()
        scanner = Mock()
        scanner.initialize.return_value = SimpleNamespace(account_name="测试")
        scanner.scan_visible_targets.return_value = SimpleNamespace(
            targets=(target,), boundary_reached=True
        )

        def runner(**kwargs):
            return SingleArticleReceipt(
                task_id="failed",
                target_fingerprint=target.fingerprint,
                status="failed",
                foreground_done=True,
                tab_closed=True,
                message="模拟捕获失败",
            )

        dispatcher = ArticleDispatcher(runner=runner)
        self.addCleanup(dispatcher.shutdown)
        MainFlowCoordinator(scanner=scanner, dispatcher=dispatcher).run(
            context, MainFlowCommand(skip_collected_records=True)
        )
        pending = pending_targets(self.db)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["title"], "尚未采到的文章")
        self.assertEqual(pending[0]["resources"]["detail"]["attempt_status"], "failed")


if __name__ == "__main__":
    import unittest

    unittest.main()


class NavigationFlowTests(ReaderFixture, unittest.TestCase):
    context = CompletionFlowTests.context
    # Reuse real database fixtures while replacing only the desktop boundary.
    def navigation(self):
        navigation = Mock()
        navigation.empty_home = False
        navigation.finish.return_value = {"status": "complete", "errors": []}
        return navigation

    def test_verified_window_passed_directly_and_cleanup_after_drain(self):
        context = self.context(); nav = self.navigation()
        scanner = Mock(); dispatcher = Mock()
        scanner.initialize.return_value = SimpleNamespace(account_name="测试")
        scanner.scan_visible_targets.return_value = SimpleNamespace(targets=(), boundary_reached=True)
        order = []
        dispatcher.wait_foreground_idle.side_effect = lambda **kw: order.append("idle") or True
        nav.finish.side_effect = lambda **kw: order.append("close") or {"status":"complete","errors":[]}
        command = MainFlowCommand(account_mode="automatic",target_account_name="测试")
        MainFlowCoordinator(scanner=scanner,dispatcher=dispatcher,navigation_factory=lambda _:nav).run(context,command)
        scanner.initialize.assert_called_once_with(command,window=nav.open.return_value)
        self.assertEqual(order,["idle","close"])
        self.assertEqual(context.state.snapshot()["status"],"completed")

    def test_navigation_error_preserved_separately_from_cleanup_error(self):
        context = self.context(); nav=self.navigation()
        nav.open.side_effect=RuntimeError("名称不唯一")
        nav.finish.return_value={"status":"failed","errors":["无法关闭"]}
        MainFlowCoordinator(scanner=Mock(),dispatcher=Mock(),navigation_factory=lambda _:nav).run(context,MainFlowCommand())
        state=context.state.snapshot()
        self.assertIn("名称不唯一",state['latestError'])
        self.assertEqual(state['cleanup']['errors'],["无法关闭"])

    def test_cancel_before_start_never_opens_wechat(self):
        context=self.context();context.cancel_token.set();factory=Mock()
        MainFlowCoordinator(scanner=Mock(),dispatcher=Mock(),navigation_factory=factory).run(context,MainFlowCommand())
        factory.assert_not_called()
        self.assertEqual(context.state.snapshot()['status'],'cancelled')

    def test_confirmed_empty_home_finishes_without_scanning(self):
        context=self.context();nav=self.navigation();nav.empty_home=True;scanner=Mock()
        MainFlowCoordinator(scanner=scanner,dispatcher=Mock(),navigation_factory=lambda _:nav).run(context,MainFlowCommand())
        scanner.initialize.assert_not_called();nav.finish.assert_called_once()
        self.assertEqual(context.state.snapshot()['progressDone'],0)
