from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SoftwareConfig:
    version: str
    data_schema_version: str


@dataclass(frozen=True, slots=True)
class StorageConfig:
    article_storage_root: Path
    db_dir: Path
    db_file_name: str
    temp_dir: Path
    log_dir: Path

    @property
    def database_path(self) -> Path:
        return self.db_dir / self.db_file_name


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    host: str
    port: int
    startup_delay_seconds: float
    verification_url: str
    confdir: Path
    ca_cert_path: Path
    enable_system_proxy: bool
    ssl_insecure: bool


@dataclass(frozen=True, slots=True)
class MitmCaptureConfig:
    ready_timeout_seconds: float
    capture_timeout_seconds: float
    result_timeout_seconds: float
    listener_shutdown_timeout_seconds: float
    close_as_capture_deadline: bool


@dataclass(frozen=True, slots=True)
class WindowConfig:
    activation_wait_seconds: float
    home_find_timeout_seconds: float
    restore_focus_after_close: bool
    article_open_timeout_seconds: float
    article_title_poll_initial_interval_seconds: float
    article_title_poll_max_interval_seconds: float
    article_title_poll_interval_seconds: float
    article_title_stable_delay_seconds: float
    article_close_confirm_timeout_seconds: float
    scroll_wheel_steps: int
    date_seek_max_steps: int
    scroll_initial_delay_seconds: float
    scroll_probe_interval_seconds: float
    scroll_probe_max_interval_seconds: float
    lazy_load_timeout_seconds: float
    unchanged_before_bounce_seconds: float
    bounce_enabled: bool
    bounce_attempts: int
    bounce_up_steps: int
    bounce_down_steps: int
    bounce_pause_seconds: float
    ui_action_interval_seconds: float = 0.5


@dataclass(frozen=True, slots=True)
class RequestConfig:
    request_interval_seconds: float
    request_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class CommentConfig:
    enabled_by_default: bool
    request_timeout_seconds: float
    page_interval_seconds: float
    max_pages: int
    max_concurrent_processes: int


@dataclass(frozen=True, slots=True)
class OfflineCacheConfig:
    enabled_by_default: bool
    max_scroll_seconds: float
    resource_timeout_seconds: float
    max_concurrent_processes: int


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    log_level: str
    auto_clean_temp_files: bool
    temp_retention_days: int
    log_retention_days: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    software: SoftwareConfig
    storage: StorageConfig
    proxy: ProxyConfig
    mitm_capture: MitmCaptureConfig
    window: WindowConfig
    request: RequestConfig
    comment: CommentConfig
    offline_cache: OfflineCacheConfig
    runtime: RuntimeConfig
