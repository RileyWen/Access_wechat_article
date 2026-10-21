from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.domain.enums import CaptureType, ProcessMessageType, ResourceType, TaskStatus


@dataclass(frozen=True, slots=True)
class TaskCommand:
    """一次文章采集任务的业务输入。"""

    target_success_count: int
    max_attempts: int
    collect_comments: bool = False
    build_offline_cache: bool = False
    skip_collected_records: bool = False
    request_interval_seconds: float = 0
    article_retry_count: int = 0

    def __post_init__(self) -> None:
        if self.target_success_count < 0:
            raise ValueError("目标成功数量不能小于 0")
        if self.max_attempts <= 0:
            raise ValueError("最大尝试次数必须大于 0")
        if self.target_success_count > 0 and self.target_success_count > self.max_attempts:
            raise ValueError("目标成功数量不能大于最大尝试次数")
        if self.article_retry_count < 0:
            raise ValueError("单篇额外重试次数不能小于 0")
        if self.request_interval_seconds < 0:
            raise ValueError("请求间隔不能小于 0")


@dataclass(frozen=True, slots=True)
class TaskContext:
    """任务运行期间保持稳定的路径、租约和取消上下文。"""

    task_id: str
    proxy_lease_id: str
    db_path: Path
    storage_root: Path
    temp_dir: Path
    started_at: datetime
    cancel_token: Any = field(default=None, repr=False, compare=False)

    def to_process_payload(self) -> dict[str, Any]:
        """转换成 multiprocessing 通道可安全传递的数据。"""
        return {
            "task_id": self.task_id,
            "proxy_lease_id": self.proxy_lease_id,
            "db_path": self.db_path.as_posix(),
            "storage_root": self.storage_root.as_posix(),
            "temp_dir": self.temp_dir.as_posix(),
            "started_at": self.started_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ArticleTarget:
    account_name: str
    title: str
    click_x: int
    click_y: int
    home_window_handle: int
    fingerprint: str
    control: Any = field(default=None, repr=False, compare=False)
    date_text: str = ""
    published_date: str = ""
    date_rect: tuple[int, int, int, int] | None = None
    title_rect: tuple[int, int, int, int] | None = None
    metric_text: str = ""
    metric_rect: tuple[int, int, int, int] | None = None
    raw_title: str = ""
    expected_article_link: str = ""


@dataclass(frozen=True, slots=True)
class ArticleDetail:
    account_name: str
    article_title: str
    published_article_time: str
    article_link: str
    article_short_link: str = ""
    ip_location: str | None = None
    audience_count: int | None = None
    read_count: int | None = None
    like_count: int | None = None
    share_count: int | None = None
    recommend_count: int | None = None
    comment_count: int | None = None


@dataclass(frozen=True, slots=True)
class MitmCaptureResult:
    task_id: str
    attempt_id: str
    status: TaskStatus
    capture_type: CaptureType
    html: str | None = None
    reference: dict[str, Any] | None = None
    request_summary: dict[str, Any] = field(default_factory=dict)
    capture_events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    error_stage: str = ""
    error_message: str = ""

    @classmethod
    def success(
        cls,
        *,
        task_id: str,
        attempt_id: str,
        html: str | None = None,
        reference: Mapping[str, Any] | None = None,
        request_summary: Mapping[str, Any] | None = None,
        capture_events: tuple[Mapping[str, Any], ...] | None = None,
    ) -> MitmCaptureResult:
        """生成成功结果；HTML 存在时优先标记为 HTML。"""
        if not html and not reference:
            raise ValueError("成功捕获结果必须包含 HTML 或 reference")
        capture_type = CaptureType.HTML if html else CaptureType.REFERENCE
        return cls(
            task_id=task_id,
            attempt_id=attempt_id,
            status=TaskStatus.SUCCESS,
            capture_type=capture_type,
            html=html,
            reference=dict(reference) if reference is not None else None,
            request_summary=dict(request_summary or {}),
            capture_events=tuple(dict(event) for event in (capture_events or ())),
        )

    @classmethod
    def failed(
        cls,
        *,
        task_id: str,
        attempt_id: str,
        error_stage: str,
        error_message: str,
        capture_events: tuple[Mapping[str, Any], ...] | None = None,
    ) -> MitmCaptureResult:
        return cls(
            task_id=task_id,
            attempt_id=attempt_id,
            status=TaskStatus.FAILED,
            capture_type=CaptureType.NONE,
            capture_events=tuple(dict(event) for event in (capture_events or ())),
            error_stage=error_stage,
            error_message=error_message,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为可通过 multiprocessing 通道发送的数据。"""
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "status": self.status.value,
            "capture_type": self.capture_type.value,
            "html": self.html,
            "reference": dict(self.reference) if self.reference is not None else None,
            "request_summary": dict(self.request_summary),
            "capture_events": [dict(event) for event in self.capture_events],
            "error_stage": self.error_stage,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MitmCaptureResult:
        reference = data.get("reference")
        request_summary = data.get("request_summary", {})
        capture_events = data.get("capture_events", [])
        if reference is not None and not isinstance(reference, Mapping):
            raise ValueError("reference 必须是映射或 null")
        if not isinstance(request_summary, Mapping):
            raise ValueError("request_summary 必须是映射")
        if not isinstance(capture_events, list):
            raise ValueError("capture_events 必须是列表")
        return cls(
            task_id=str(data.get("task_id", "")),
            attempt_id=str(data.get("attempt_id", "")),
            status=TaskStatus(str(data.get("status", ""))),
            capture_type=CaptureType(str(data.get("capture_type", ""))),
            html=None if data.get("html") is None else str(data.get("html")),
            reference=dict(reference) if reference is not None else None,
            request_summary=dict(request_summary),
            capture_events=tuple(dict(event) for event in capture_events if isinstance(event, Mapping)),
            error_stage=str(data.get("error_stage", "")),
            error_message=str(data.get("error_message", "")),
        )


@dataclass(frozen=True, slots=True)
class ResourceManifest:
    resource_types: tuple[ResourceType, ...] = ()

    @classmethod
    def from_types(cls, resource_types: Iterable[ResourceType]) -> ResourceManifest:
        unique_types = sorted(set(resource_types), key=lambda item: item.value)
        return cls(resource_types=tuple(unique_types))

    def to_json_values(self) -> list[str]:
        return [item.value for item in self.resource_types]


@dataclass(frozen=True, slots=True)
class ProcessMessage:
    task_id: str
    attempt_id: str
    message_type: ProcessMessageType
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id 不能为空")
        if not self.attempt_id.strip():
            raise ValueError("attempt_id 不能为空")
        if self.message_type is ProcessMessageType.START_CAPTURE:
            proxy_lease_id = str(self.payload.get("proxy_lease_id", "")).strip()
            if not proxy_lease_id:
                raise ValueError("START_CAPTURE 必须包含 proxy_lease_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "message_type": self.message_type.value,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProcessMessage:
        payload = data.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("payload 必须是映射")
        return cls(
            task_id=str(data.get("task_id", "")),
            attempt_id=str(data.get("attempt_id", "")),
            message_type=ProcessMessageType(str(data.get("message_type", ""))),
            payload=dict(payload),
        )
