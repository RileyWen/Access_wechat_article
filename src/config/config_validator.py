from __future__ import annotations

import re
from pathlib import Path

from src.config.app_config import AppConfig
from src.domain.enums import ErrorCode
from src.domain.errors import DomainError


SCHEMA_VERSION_PATTERN = re.compile(r"^v\d+\.\d+$")
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR"}


class ConfigValidationError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.INVALID_CONFIG, message)


def validate_app_config(config: AppConfig) -> None:
    """集中校验会改变运行路径和任务截止条件的配置。"""
    if not SCHEMA_VERSION_PATTERN.fullmatch(config.software.data_schema_version):
        raise ConfigValidationError("software.data_schema_version 必须形如 v2.1")

    db_file_name = config.storage.db_file_name
    if Path(db_file_name).name != db_file_name or Path(db_file_name).suffix.lower() != ".sqlite3":
        raise ConfigValidationError("storage.db_file_name 必须是单独的 .sqlite3 文件名")

    for field_name, path in (
        ("article_storage_root", config.storage.article_storage_root),
        ("db_dir", config.storage.db_dir),
        ("temp_dir", config.storage.temp_dir),
        ("log_dir", config.storage.log_dir),
        ("confdir", config.proxy.confdir),
        ("ca_cert_path", config.proxy.ca_cert_path),
    ):
        if not str(path).strip():
            raise ConfigValidationError(f"{field_name} 不能为空")

    if not config.proxy.host.strip():
        raise ConfigValidationError("proxy.host 不能为空")
    if not 1 <= config.proxy.port <= 65535:
        raise ConfigValidationError("proxy.port 必须在 1 到 65535 之间")

    positive_values = (
        ("mitm_capture.ready_timeout_seconds", config.mitm_capture.ready_timeout_seconds),
        ("mitm_capture.capture_timeout_seconds", config.mitm_capture.capture_timeout_seconds),
        ("mitm_capture.result_timeout_seconds", config.mitm_capture.result_timeout_seconds),
        (
            "mitm_capture.listener_shutdown_timeout_seconds",
            config.mitm_capture.listener_shutdown_timeout_seconds,
        ),
        ("request.request_timeout_seconds", config.request.request_timeout_seconds),
        ("comment.request_timeout_seconds", config.comment.request_timeout_seconds),
        ("offline_cache.resource_timeout_seconds", config.offline_cache.resource_timeout_seconds),
        ("window.article_open_timeout_seconds", config.window.article_open_timeout_seconds),
        ("window.home_find_timeout_seconds", config.window.home_find_timeout_seconds),
        (
            "window.article_title_poll_initial_interval_seconds",
            config.window.article_title_poll_initial_interval_seconds,
        ),
        (
            "window.article_title_poll_max_interval_seconds",
            config.window.article_title_poll_max_interval_seconds,
        ),
        (
            "window.article_title_poll_interval_seconds",
            config.window.article_title_poll_interval_seconds,
        ),
        (
            "window.article_close_confirm_timeout_seconds",
            config.window.article_close_confirm_timeout_seconds,
        ),
        ("window.scroll_probe_interval_seconds", config.window.scroll_probe_interval_seconds),
        (
            "window.scroll_probe_max_interval_seconds",
            config.window.scroll_probe_max_interval_seconds,
        ),
        ("window.lazy_load_timeout_seconds", config.window.lazy_load_timeout_seconds),
    )
    for field_name, value in positive_values:
        if value <= 0:
            raise ConfigValidationError(f"{field_name} 必须大于 0")

    import math
    if not math.isfinite(config.window.ui_action_interval_seconds) or config.window.ui_action_interval_seconds < .5:
        raise ConfigValidationError("界面操作间隔不能小于 0.5 秒")

    non_negative_values = (
        ("proxy.startup_delay_seconds", config.proxy.startup_delay_seconds),
        ("window.activation_wait_seconds", config.window.activation_wait_seconds),
        (
            "window.article_title_stable_delay_seconds",
            config.window.article_title_stable_delay_seconds,
        ),
        ("window.scroll_initial_delay_seconds", config.window.scroll_initial_delay_seconds),
        (
            "window.unchanged_before_bounce_seconds",
            config.window.unchanged_before_bounce_seconds,
        ),
        ("window.bounce_pause_seconds", config.window.bounce_pause_seconds),
        ("request.request_interval_seconds", config.request.request_interval_seconds),
        ("comment.page_interval_seconds", config.comment.page_interval_seconds),
        ("offline_cache.max_scroll_seconds", config.offline_cache.max_scroll_seconds),
    )
    for field_name, value in non_negative_values:
        if value < 0:
            raise ConfigValidationError(f"{field_name} 不能小于 0")

    integer_limits = (
        ("comment.max_pages", config.comment.max_pages, 1),
        ("comment.max_concurrent_processes", config.comment.max_concurrent_processes, 1),
        (
            "offline_cache.max_concurrent_processes",
            config.offline_cache.max_concurrent_processes,
            1,
        ),
        ("runtime.temp_retention_days", config.runtime.temp_retention_days, 0),
        ("runtime.log_retention_days", config.runtime.log_retention_days, 0),
        ("window.scroll_wheel_steps", config.window.scroll_wheel_steps, 1),
        ("window.date_seek_max_steps", config.window.date_seek_max_steps, 1),
        ("window.bounce_attempts", config.window.bounce_attempts, 0),
        ("window.bounce_up_steps", config.window.bounce_up_steps, 1),
        ("window.bounce_down_steps", config.window.bounce_down_steps, 1),
    )
    for field_name, value, minimum in integer_limits:
        if value < minimum:
            comparison = "大于 0" if minimum == 1 else "不能小于 0"
            raise ConfigValidationError(f"{field_name} {comparison}")

    if config.comment.max_concurrent_processes > 10:
        raise ConfigValidationError("comment.max_concurrent_processes 不能大于 10")
    if config.offline_cache.max_concurrent_processes > 10:
        raise ConfigValidationError("offline_cache.max_concurrent_processes 不能大于 10")
    if config.window.date_seek_max_steps < config.window.scroll_wheel_steps:
        raise ConfigValidationError(
            "window.date_seek_max_steps 不能小于 scroll_wheel_steps"
        )

    if not config.mitm_capture.close_as_capture_deadline:
        raise ConfigValidationError("mitm_capture.close_as_capture_deadline 当前必须为 true")
    if (
        config.window.scroll_probe_max_interval_seconds
        < config.window.scroll_probe_interval_seconds
    ):
        raise ConfigValidationError(
            "window.scroll_probe_max_interval_seconds 不能小于 scroll_probe_interval_seconds"
        )
    if (
        config.window.article_title_poll_max_interval_seconds
        < config.window.article_title_poll_initial_interval_seconds
    ):
        raise ConfigValidationError(
            "window.article_title_poll_max_interval_seconds 不能小于 article_title_poll_initial_interval_seconds"
        )
    article_window_budget = (
        config.window.article_open_timeout_seconds
        + config.window.article_title_stable_delay_seconds
        + config.window.article_close_confirm_timeout_seconds
    )
    if article_window_budget >= config.mitm_capture.capture_timeout_seconds:
        raise ConfigValidationError(
            "文章打开、标题稳定和关闭确认的总窗口预算必须小于 "
            "mitm_capture.capture_timeout_seconds"
        )
    if config.runtime.log_level not in VALID_LOG_LEVELS:
        raise ConfigValidationError("runtime.log_level 只允许 DEBUG、INFO、WARN、ERROR")
