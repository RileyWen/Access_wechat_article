from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    """可直接序列化为字符串的枚举基类。"""

    def __str__(self) -> str:
        return self.value


class TaskStatus(StringEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class TaskStage(StringEnum):
    DB_INIT = "db_init"
    PREFLIGHT = "preflight"
    HOME_SCAN = "home_scan"
    SINGLE_CAPTURE = "single_capture"
    MITM_CAPTURE = "mitm_capture"
    HTML_SAVE = "html_save"
    COMMENT_COLLECT = "comment_collect"
    OFFLINE_CACHE = "offline_cache"
    EXPORT = "export"
    CLEANUP = "cleanup"


class CaptureType(StringEnum):
    HTML = "html"
    REFERENCE = "reference"
    NONE = "none"


class ResourceType(StringEnum):
    ARTICLE_DETAIL = "article_detail"
    ORIGIN_HTML = "origin_html"
    ORIGIN_REQUEST = "origin_request"
    COMMENT_DETAIL = "comment_detail"
    COMMENT_ASSETS = "comment_assets"
    READER_HTML = "reader_html"
    READER_ASSETS = "reader_assets"
    OFFLINE_HTML = "offline_html"
    OFFLINE_ASSETS = "offline_assets"


class ProcessMessageType(StringEnum):
    START_CAPTURE = "start_capture"
    PROXY_SNAPSHOT = "proxy_snapshot"
    PROGRESS = "progress"
    READY = "ready"
    STOP_CAPTURE = "stop_capture"
    RESULT = "result"
    CANCEL = "cancel"
    FAILED = "failed"


class ErrorCode(StringEnum):
    INVALID_CONFIG = "invalid_config"
    DB_INIT_FAILED = "db_init_failed"
    DB_UNAVAILABLE = "db_unavailable"
    PREFLIGHT_FAILED = "preflight_failed"
    PROCESS_START_FAILED = "process_start_failed"
    MITM_NOT_READY = "mitm_not_ready"
    PROXY_FAILED = "proxy_failed"
    WINDOW_NOT_FOUND = "window_not_found"
    ARTICLE_TITLE_MISMATCH = "article_title_mismatch"
    CAPTURE_EMPTY = "capture_empty"
    REFERENCE_REQUEST_FAILED = "reference_request_failed"
    PARSE_FAILED = "parse_failed"
    SAVE_FAILED = "save_failed"
    COMMENT_FETCH_FAILED = "comment_fetch_failed"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"
