from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any, Callable


class TaskRuntimeTracker:
    """记录主流程和并发文章子任务的轻量实时状态。"""

    def __init__(
        self,
        *,
        progress_total_label: str = "全部",
        current_action: str = "设置公众号和采集范围后开始运行",
        account_name: str = "等待识别",
        task_info: str = "待获取",
        proxy_status_label: str = "空闲",
        runtime_logger: Any | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._lock = RLock()
        self._now = now
        self._current_action = str(current_action or "")
        self._account_name = str(account_name or "等待识别")
        self._task_info = str(task_info or "待获取")
        self._proxy_status_label = str(proxy_status_label or "空闲")
        self._runtime_logger = runtime_logger
        self._progress_total_label = self._normalize_total_label(progress_total_label)
        self._average_total_seconds = 0.0
        self._average_count = 0
        self._active_worker_count = 0
        self._total_worker_count = 0
        self._error_count = 0
        self._latest_error = ""
        self._article_sequence = 0
        self._current_article_key = ""
        self._article_records: dict[str, dict[str, Any]] = {}
        self._article_order: list[str] = []
        self._article_error_keys: set[str] = set()

    @classmethod
    def default_snapshot(cls, *, proxy_status_label: str = "空闲") -> dict[str, Any]:
        return cls(proxy_status_label=proxy_status_label).snapshot()

    def set_total_label(self, value: str | int) -> None:
        with self._lock:
            self._progress_total_label = self._normalize_total_label(value)

    def set_action(self, action: str) -> None:
        text = str(action or "").strip()
        if text and not text.startswith("正在"):
            text = f"正在{text}"
        with self._lock:
            self._current_action = text
        self._log_detail("INFO", text, source="task-action")

    def set_terminal_action(self, status: Any) -> None:
        """任务结束后写入终态动作，避免界面停留在资源清理阶段。"""
        value = str(getattr(status, "value", status) or "").lower()
        if value == "success":
            text = "采集任务完成"
        elif value == "cancelled":
            text = "采集任务已停止"
        elif value == "failed":
            text = "采集任务异常"
        else:
            text = "采集任务结束"
        with self._lock:
            self._current_action = text
        self._log_detail("INFO", text, source="task-action")

    def set_account_name(self, account_name: str) -> None:
        text = str(account_name or "").strip() or "等待识别"
        with self._lock:
            self._account_name = text

    def set_task_info(self, task_info: str) -> None:
        text = str(task_info or "").strip() or "待获取"
        with self._lock:
            self._task_info = text

    def set_proxy_status(self, label: str) -> None:
        text = str(label or "").strip() or "空闲"
        with self._lock:
            self._proxy_status_label = text

    def start_article(
        self,
        *,
        article_key: str | None = None,
        task_info: str | None = None,
        total_label: str | int | None = None,
        collect_comments: bool = False,
    ) -> str:
        with self._lock:
            if total_label is not None:
                self._progress_total_label = self._normalize_total_label(total_label)
            if task_info is not None:
                self._task_info = str(task_info or "").strip() or "待获取"
            key = str(article_key or "").strip()
            if not key:
                self._article_sequence += 1
                key = f"article-{self._article_sequence}"
            if key not in self._article_records:
                self._article_records[key] = {
                    "articleKey": key,
                    "taskInfo": self._task_info,
                    "status": "running",
                    "currentStage": "",
                    "collectComments": bool(collect_comments),
                    "startedAt": self._timestamp(),
                    "finishedAt": "",
                    "durationSeconds": None,
                    "errorMessage": "",
                    "stages": [],
                    "averageCounted": False,
                    "summaryLogged": False,
                }
                self._article_order.append(key)
            else:
                record = self._article_records[key]
                record["taskInfo"] = self._task_info
                record["collectComments"] = bool(collect_comments)
                record["status"] = "running"
            self._current_article_key = key
            article_title = self._article_records[key]["taskInfo"]
        self._log_detail(
            "INFO",
            "开始处理文章",
            source="article-flow",
            context={
                "article_key": key,
                "article_title": article_title,
                "collect_comments": bool(collect_comments),
            },
        )
        return key

    def record_article_stage(
        self,
        *,
        article_key: str,
        stage: str,
        label: str,
        status: str,
        duration_seconds: float = 0.0,
        message: str = "",
    ) -> None:
        key = str(article_key or "").strip()
        if not key:
            return
        with self._lock:
            if key not in self._article_records:
                self.start_article(article_key=key, task_info=self._task_info)
            record = self._article_records[key]
            label_text = str(label or stage or "运行步骤").strip()
            record["currentStage"] = label_text
            record["stages"].append(
                {
                    "stage": str(stage or ""),
                    "label": label_text,
                    "status": str(status or "running"),
                    "durationSeconds": max(0.0, float(duration_seconds)),
                    "message": str(message or ""),
                    "createdAt": self._timestamp(),
                }
            )
        self._log_detail(
            "ERROR" if str(status).lower() == "failed" else "INFO",
            f"{label_text}：{message or status}",
            source="article-stage",
            context={
                "article_key": key,
                "stage": str(stage or ""),
                "status": str(status or "running"),
                "duration_seconds": max(0.0, float(duration_seconds)),
            },
        )

    def finish_article(
        self,
        *,
        article_key: str | None = None,
        duration_seconds: float = 0.0,
        count_for_average: bool = False,
        status: str = "success",
    ) -> None:
        with self._lock:
            key = str(article_key or self._current_article_key or "").strip()
            record = self._article_records.get(key)
            if record is None:
                return
            record["status"] = str(status or "success")
            record["finishedAt"] = self._timestamp()
            record["durationSeconds"] = max(0.0, float(duration_seconds))
            if count_for_average and not record["averageCounted"]:
                self._average_total_seconds += record["durationSeconds"]
                self._average_count += 1
                record["averageCounted"] = True
            if self._current_article_key == key:
                self._current_article_key = ""
            should_write_summary = not bool(record["summaryLogged"])
            record["summaryLogged"] = True
            article_index = self._article_order.index(key) + 1
            record_snapshot = {
                field: ([dict(stage) for stage in value] if field == "stages" else value)
                for field, value in record.items()
                if field not in {"averageCounted", "summaryLogged"}
            }
            total_label = self._progress_total_label
        if not should_write_summary:
            return
        self._write_article_summary(
            record_snapshot,
            article_index=article_index,
            total_label=total_label,
        )

    def worker_started(self) -> None:
        with self._lock:
            self._active_worker_count += 1
            self._total_worker_count += 1
            active_count = self._active_worker_count
            total_count = self._total_worker_count
        self._log_detail(
            "INFO",
            "子进程已启动",
            source="worker",
            context={"active_count": active_count, "total_count": total_count},
        )

    def worker_finished(self) -> None:
        with self._lock:
            self._active_worker_count = max(0, self._active_worker_count - 1)
            active_count = self._active_worker_count
            total_count = self._total_worker_count
        self._log_detail(
            "INFO",
            "子进程已结束",
            source="worker",
            context={"active_count": active_count, "total_count": total_count},
        )

    def record_article_error(self, article_key: str, message: str) -> None:
        key = str(article_key or "").strip()
        text = str(message or "").strip() or "未知异常"
        if not key:
            self.record_error(text)
            return
        with self._lock:
            if key not in self._article_records:
                self.start_article(article_key=key, task_info=self._task_info)
            record = self._article_records[key]
            if not record["errorMessage"]:
                record["errorMessage"] = text
            if key in self._article_error_keys:
                return
            self._article_error_keys.add(key)
            self._error_count += 1
            self._latest_error = text
        self._log_detail(
            "ERROR",
            text,
            source="article-error",
            context={"article_key": key},
        )

    def record_error(self, message: str) -> None:
        text = str(message or "").strip() or "未知异常"
        with self._lock:
            self._error_count += 1
            self._latest_error = text
        self._log_detail("ERROR", text, source="task-error")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            progress_done = len(self._article_order)
            average_seconds = (
                self._average_total_seconds / self._average_count
                if self._average_count > 0
                else None
            )
            article_records = []
            for key in self._article_order:
                record = self._article_records[key]
                article_records.append(
                    {
                        field: ([dict(stage) for stage in value] if field == "stages" else value)
                        for field, value in record.items()
                        if field not in {"averageCounted", "summaryLogged"}
                    }
                )
            return {
                "currentAction": self._current_action,
                "accountName": self._account_name,
                "taskInfo": self._task_info,
                "proxyStatusLabel": self._proxy_status_label,
                "progressDone": progress_done,
                "progressTotalLabel": self._progress_total_label,
                "progressPercent": self._progress_percent(progress_done, self._progress_total_label),
                "averageArticleSeconds": average_seconds,
                "averageArticleDurationLabel": self._format_seconds(average_seconds),
                "activeWorkerCount": self._active_worker_count,
                "totalWorkerCount": self._total_worker_count,
                "errorCount": self._error_count,
                "latestError": self._latest_error,
                "articleRecords": article_records,
            }

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _write_article_summary(
        self,
        record: dict[str, Any],
        *,
        article_index: int,
        total_label: str,
    ) -> None:
        status = str(record.get("status") or "success").lower()
        stages = list(record.get("stages") or [])
        html_saved = any(
            stage.get("stage") == "html" and str(stage.get("status")).lower() == "success"
            for stage in stages
        )
        comment_failed = any(
            stage.get("stage") == "comment" and str(stage.get("status")).lower() == "failed"
            for stage in stages
        )
        comment_succeeded = any(
            stage.get("stage") == "comment" and str(stage.get("status")).lower() == "success"
            for stage in stages
        )
        title = str(record.get("taskInfo") or "未识别标题")
        duration = max(0.0, float(record.get("durationSeconds") or 0.0))

        if status == "success":
            result_text = "详情已保存" if html_saved else "文章处理完成"
            if bool(record.get("collectComments")) and comment_succeeded:
                result_text += "｜评论采集完成"
            level = "SUCCESS"
        else:
            if html_saved and (comment_failed or bool(record.get("collectComments"))):
                result_text = "详情已保存，评论采集失败"
            else:
                failed_stage = next(
                    (
                        str(stage.get("label") or "文章处理")
                        for stage in reversed(stages)
                        if str(stage.get("status")).lower() == "failed"
                    ),
                    "文章处理",
                )
                result_text = f"{failed_stage}失败"
            level = "ERROR"

        message = (
            f"[{article_index}/{total_label}] {title}｜{result_text}｜耗时 {duration:.1f} 秒"
        )
        context = {
            "article_key": record.get("articleKey") or "",
            "status": status,
            "error_message": record.get("errorMessage") or "",
        }
        if level == "ERROR":
            self._log_error(message, source="article-flow", context=context)
        else:
            self._log_summary(level, message, source="article-flow", context=context)

    def _log_summary(
        self,
        level: str,
        message: str,
        *,
        source: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        method = getattr(self._runtime_logger, "write_summary", None)
        if callable(method):
            try:
                method(level, message, source=source, context=context)
            except Exception:
                pass

    def _log_detail(
        self,
        level: str,
        message: str,
        *,
        source: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        method = getattr(self._runtime_logger, "write_detail", None)
        if callable(method):
            try:
                method(level, message, source=source, context=context)
            except Exception:
                pass

    def _log_error(
        self,
        message: str,
        *,
        source: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        method = getattr(self._runtime_logger, "write_error", None)
        if callable(method):
            try:
                method(message, source=source, context=context)
            except Exception:
                pass

    @staticmethod
    def _normalize_total_label(value: str | int) -> str:
        text = str(value or "").strip()
        return text if text else "全部"

    @staticmethod
    def _progress_percent(done: int, total_label: str) -> int:
        try:
            total = int(total_label)
        except (TypeError, ValueError):
            return 0
        if total <= 0:
            return 0
        return min(100, max(0, round((done / total) * 100)))

    @staticmethod
    def _format_seconds(value: float | None) -> str:
        if value is None:
            return "待统计"
        return f"{value:.1f} 秒"


__all__ = ["TaskRuntimeTracker"]
