from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.config.app_config import AppConfig
from src.modules.window.ui_action_pacer import UiActionPacer, PacedComponent, ObservedTabService
from src.modules.window.article_card_reader import UiaArticleCardReader
from src.modules.window.article_clicker import ArticleClicker
from src.modules.window.home_article_cursor import HomeArticleCursor
from src.modules.window.home_window_activation import WindowsHomeWindowGuard
from src.modules.window.wechat_browser_tabs import (
    UiaWechatBrowserTabAdapter,
    WechatBrowserTabService,
)
from src.modules.window.wechat_home_reader import WechatHomeReader
from src.modules.window.wechat_home_scroller import WechatHomeScroller
from src.modules.window.wechat_home_window_finder import find_wechat_home_window
from src.modules.window.uia_window_test_reader import UiaWindowTestReader


class WindowRuntimeFactory:
    """只使用内存 AppConfig，统一装配文章窗口操作组件。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        reader_factory: Callable[..., Any] = UiaArticleCardReader,
        scroller_factory: Callable[..., Any] = WechatHomeScroller,
        cursor_factory: Callable[..., Any] = HomeArticleCursor,
        window_test_reader_factory: Callable[..., Any] = UiaWindowTestReader,
        home_finder: Callable[..., Any] = find_wechat_home_window,
        home_reader_factory: Callable[..., Any] = WechatHomeReader,
        guard_factory: Callable[..., Any] = WindowsHomeWindowGuard,
        clicker_factory: Callable[..., Any] = ArticleClicker,
        tab_adapter_factory: Callable[..., Any] = UiaWechatBrowserTabAdapter,
        tab_service_factory: Callable[..., Any] = WechatBrowserTabService,
    ) -> None:
        self.article_opened = None
        self._window = config.window
        self.pacer = UiActionPacer(getattr(config.window, "ui_action_interval_seconds", .5))
        self._reader_factory = reader_factory
        self._scroller_factory = scroller_factory
        self._cursor_factory = cursor_factory
        self._window_test_reader_factory = window_test_reader_factory
        self._home_finder = home_finder
        self._home_reader_factory = home_reader_factory
        self._guard_factory = guard_factory
        self._clicker_factory = clicker_factory
        self._tab_adapter_factory = tab_adapter_factory
        self._tab_service_factory = tab_service_factory

    @property
    def restore_focus_after_close(self) -> bool:
        return self._window.restore_focus_after_close

    def create_reader(self) -> Any:
        return self._reader_factory()

    def find_home_window(
        self,
        *,
        reader: Any,
        timeout_seconds: float | None = None,
    ) -> Any:
        return self._home_finder(
            timeout_seconds=timeout_seconds,
        )

    def create_home_reader(self) -> Any:
        return self._home_reader_factory()

    def create_window_test_reader(self) -> Any:
        """窗口测试使用独立 UIA 快照读取器，不复用主流程游标。"""

        return self._window_test_reader_factory()

    def create_scroller(self) -> Any:
        """按当前主页滚动设置创建一次无鼠标移动的滚动器。"""

        scroller = self._scroller_factory(
            wheel_steps=self._window.scroll_wheel_steps,
            activation_wait_seconds=self._window.activation_wait_seconds,
        )
        return PacedComponent(scroller, self.pacer, ("scroll", "scroll_up", "scroll_down"))

    def create_cursor(
        self,
        *,
        reader: Any,
        account_name: str,
        trace: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        scroller = self.create_scroller()
        return self._cursor_factory(
            reader=reader,
            account_name=account_name,
            scroller=scroller,
            scroll_wait_seconds=self._window.scroll_initial_delay_seconds,
            scroll_probe_interval_seconds=self._window.scroll_probe_interval_seconds,
            scroll_probe_max_interval_seconds=(
                self._window.scroll_probe_max_interval_seconds
            ),
            lazy_load_timeout_seconds=self._window.lazy_load_timeout_seconds,
            unchanged_before_bounce_seconds=(
                self._window.unchanged_before_bounce_seconds
            ),
            bounce_enabled=self._window.bounce_enabled,
            bounce_attempts=self._window.bounce_attempts,
            bounce_up_steps=self._window.bounce_up_steps,
            bounce_down_steps=self._window.bounce_down_steps,
            bounce_pause_seconds=self._window.bounce_pause_seconds,
            trace=trace,
        )

    def create_home_guard(self) -> Any:
        return self._guard_factory(
            activation_wait_seconds=self._window.activation_wait_seconds,
        )

    def create_clicker(self) -> Any:
        return PacedComponent(self._clicker_factory(), self.pacer, ("click", "click_point"))

    def create_tab_service(self) -> Any:
        adapter = self._tab_adapter_factory(
            document_return_timeout_seconds=(
                self._window.article_close_confirm_timeout_seconds
            ),
        )
        return ObservedTabService(self._tab_service_factory(adapter=adapter), self.pacer, self)


__all__ = ["WindowRuntimeFactory"]
