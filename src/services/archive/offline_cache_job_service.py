from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re
import shutil
from threading import RLock, Thread, current_thread
import time
from typing import Any, Iterable
from uuid import uuid4

from src.domain.enums import TaskStatus
from src.domain.models import ResourceManifest
from src.modules.archive.resource_manifest_builder import ResourceManifestBuilder
from src.services.archive.offline_cache_process_control_service import (
    OfflineCacheProcessControlService,
    OfflineCacheProcessError,
)
from src.services.archive.resource_commit_service import ResourceCommitService
from src.services.runtime.database_write_coordinator import DatabaseWriteCoordinator
from src.storage.repositories.article_repository import (
    ArticleRepository,
    OfflineCacheArticleRecord,
)
from src.storage.repositories.fetch_history_repository import (
    FetchHistoryRepository,
    FetchHistoryWrite,
)
from src.storage.sqlite.connection import sqlite_connection


WECHAT_SHORT_LINK_PATTERN = re.compile(r"https://mp\.weixin\.qq\.com/s/[A-Za-z0-9_-]+")


def has_complete_offline_cache(article_directory: str | Path) -> bool:
    """以真实文件为准判断文章是否已经具备可用的离线缓存。"""
    root = Path(article_directory)
    return (root / "index.html").is_file() and (root / "assets").is_dir()


@dataclass(frozen=True, slots=True)
class OfflineCacheResultItem:
    article_id: int
    article_title: str
    ok: bool
    status: str
    message: str
    archive_dir: str = ""
    index_html_path: str = ""
    resource_count: int = 0
    warning: str = ""
    elapsed_seconds: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "articleId": self.article_id,
            "articleTitle": self.article_title,
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "archiveDir": self.archive_dir,
            "indexHtmlPath": self.index_html_path,
            "resourceCount": self.resource_count,
            "warning": self.warning,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
        }


@dataclass(frozen=True, slots=True)
class OfflineCacheProgressItem:
    """记录单篇离线缓存任务的当前状态，供批次进度接口展示。"""

    article_id: int
    article_title: str
    status: str = "queued"
    step: str = "等待执行"
    elapsed_seconds: float = 0.0
    started_monotonic: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "articleId": self.article_id,
            "articleTitle": self.article_title,
            "status": self.status,
            "step": self.step,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
        }


@dataclass(slots=True)
class OfflineCacheJob:
    job_id: str
    targets: tuple[OfflineCacheArticleRecord, ...]
    concurrency: int
    skipped: int = 0
    status: str = "pending"
    finished: int = 0
    running: int = 0
    message: str = ""
    results: list[OfflineCacheResultItem] = field(default_factory=list)
    progress_items: dict[int, OfflineCacheProgressItem] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.targets)

    def to_payload(self) -> dict[str, Any]:
        requested_total = self.total + self.skipped
        processed = self.finished + self.skipped
        queued = max(self.total - self.finished - self.running, 0)
        failed = sum(1 for item in self.results if not item.ok)
        active_processes = [
            item.to_payload()
            for item in self.progress_items.values()
            if item.status == "running"
        ]
        return {
            "ok": self.status not in {"failed", "missing"},
            "jobId": self.job_id,
            "status": self.status,
            "total": self.total,
            "finished": self.finished,
            "running": self.running,
            "skipped": self.skipped,
            "requestedTotal": requested_total,
            "processed": processed,
            "queued": queued,
            "failed": failed,
            "activeProcesses": active_processes,
            "concurrency": self.concurrency,
            "results": [item.to_payload() for item in self.results],
            "message": self.message,
        }


class OfflineCacheConflictError(RuntimeError):
    pass


class OfflineCacheJobService:
    """管理数据档案页的离线缓存批次，并串行提交最终文件和 SQLite。"""

    def __init__(
        self,
        *,
        database_path: str | Path,
        storage_root: str | Path,
        temp_root: str | Path,
        browser_cache_dir: str | Path,
        max_concurrent_processes: int,
        max_scroll_seconds: float,
        resource_timeout_seconds: float,
        process_control: Any | None = None,
        write_coordinator: DatabaseWriteCoordinator | None = None,
        resource_commit: ResourceCommitService | None = None,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        self.storage_root = Path(storage_root).resolve()
        self.temp_root = Path(temp_root).resolve()
        self.browser_cache_dir = Path(browser_cache_dir).resolve()
        self.max_concurrent_processes = max(1, int(max_concurrent_processes))
        self.max_scroll_seconds = max(0.0, float(max_scroll_seconds))
        self.resource_timeout_seconds = max(0.1, float(resource_timeout_seconds))
        self._process_control = process_control or OfflineCacheProcessControlService()
        self._write_coordinator = write_coordinator or DatabaseWriteCoordinator()
        self._resource_commit = resource_commit or ResourceCommitService(
            write_coordinator=self._write_coordinator
        )
        self._jobs: dict[str, OfflineCacheJob] = {}
        self._active_attempts: set[Any] = set()
        self._job_threads: dict[str, Thread] = {}
        self._closed = False
        self._lock = RLock()

    def create_articles_job(
        self,
        article_ids: Iterable[int],
        *,
        start: bool = True,
    ) -> OfflineCacheJob:
        with sqlite_connection(self.database_path, write=False) as connection:
            targets = ArticleRepository(connection).list_offline_cache_records_by_ids(
                article_ids
            )
        return self._create_job(targets, skipped=0, start=start)

    def create_account_job(
        self, account_id: int, *, start: bool = True
    ) -> OfflineCacheJob:
        with sqlite_connection(self.database_path, write=False) as connection:
            records = ArticleRepository(
                connection
            ).list_offline_cache_records_by_account(account_id)
        targets: list[OfflineCacheArticleRecord] = []
        skipped = 0
        for record in records:
            try:
                article_directory = self._resolve_article_directory(record.archive_dir)
            except ValueError:
                targets.append(record)
                continue
            from src.services.archive.resource_state_service import (
                audit_article,
                needed,
            )

            _, resource_state = audit_article(
                self.database_path, self.storage_root, record.id
            )
            if not needed(resource_state, ["offline"]):
                skipped += 1
            else:
                targets.append(record)
        return self._create_job(tuple(targets), skipped=skipped, start=start)

    def _create_job(
        self,
        targets: tuple[OfflineCacheArticleRecord, ...],
        *,
        skipped: int,
        start: bool,
    ) -> OfflineCacheJob:
        with self._lock:
            if self._closed:
                raise RuntimeError("离线缓存任务服务已经关闭")
            if any(job.status in {"pending", "running"} for job in self._jobs.values()):
                raise OfflineCacheConflictError("已有离线缓存任务正在执行")
            job = OfflineCacheJob(
                job_id=f"offline-cache-{uuid4().hex[:12]}",
                targets=targets,
                concurrency=min(self.max_concurrent_processes, max(1, len(targets))),
                skipped=max(0, int(skipped)),
                status="pending" if targets else "done",
                message=self._empty_job_message(skipped) if not targets else "",
                progress_items={
                    target.id: OfflineCacheProgressItem(
                        article_id=target.id,
                        article_title=target.article_title,
                    )
                    for target in targets
                },
            )
            self._jobs[job.job_id] = job
            if start and targets:
                job_thread = Thread(
                    target=self.run_job,
                    args=(job.job_id,),
                    name=f"awa-offline-cache-job-{job.job_id}",
                    daemon=True,
                )
                self._job_threads[job.job_id] = job_thread
                job_thread.start()
        return self.get_job(job.job_id)

    def run_job(self, job_id: str) -> None:
        try:
            self._execute_job(job_id)
        finally:
            with self._lock:
                if self._job_threads.get(job_id) is current_thread():
                    self._job_threads.pop(job_id, None)

    def _execute_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "pending":
                return
            if self._closed:
                job.status = "failed"
                job.running = 0
                job.message = "离线缓存任务服务已关闭，未启动子进程。"
                return
            job.status = "running"
        try:
            with ThreadPoolExecutor(
                max_workers=job.concurrency,
                thread_name_prefix="awa-offline-cache-monitor",
            ) as executor:
                futures = {
                    executor.submit(self._run_target, job, target): target
                    for target in job.targets
                }
                for future in as_completed(futures):
                    target = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = OfflineCacheResultItem(
                            article_id=target.id,
                            article_title=target.article_title,
                            ok=False,
                            status="failed",
                            message=f"离线缓存任务异常：{type(exc).__name__}: {exc}",
                        )
                    with self._lock:
                        job.results.append(result)
                        job.finished += 1
                        self._update_progress_item(
                            job,
                            target,
                            status="success" if result.ok else "failed",
                            step="缓存完成" if result.ok else result.message,
                            elapsed_seconds=result.elapsed_seconds,
                        )
                        self._sync_running_count(job)
        except Exception as exc:
            with self._lock:
                job.status = "failed"
                job.running = 0
                job.message = f"离线缓存批次失败：{type(exc).__name__}: {exc}"
                for target in job.targets:
                    current = job.progress_items.get(target.id)
                    if current is not None and current.status in {"queued", "running"}:
                        self._update_progress_item(
                            job,
                            target,
                            status="failed",
                            step=job.message,
                            elapsed_seconds=current.elapsed_seconds,
                        )
            return

        with self._lock:
            failed = sum(1 for item in job.results if not item.ok)
            job.status = "done" if failed == 0 else "partial_failed"
            job.running = 0
            job.message = self._completion_message(job, failed)

    def get_job(self, job_id: str) -> OfflineCacheJob:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                raise KeyError(job_id)
            return OfflineCacheJob(
                job_id=job.job_id,
                targets=job.targets,
                concurrency=job.concurrency,
                skipped=job.skipped,
                status=job.status,
                finished=job.finished,
                running=job.running,
                message=job.message,
                results=list(job.results),
                progress_items=dict(job.progress_items),
            )

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            attempts = tuple(self._active_attempts)
            job_threads = tuple(self._job_threads.values())
        for attempt in attempts:
            try:
                attempt.cancel()
            except Exception:
                pass
        for job_thread in job_threads:
            if job_thread is current_thread():
                continue
            job_thread.join(timeout=5.0)
        shutil.rmtree(self.temp_root / "offline-cache", ignore_errors=True)

    def is_busy(self) -> bool:
        with self._lock:
            return any(
                job.status in {"pending", "running"} for job in self._jobs.values()
            )

    def _run_target(
        self,
        job: OfflineCacheJob,
        target: OfflineCacheArticleRecord,
    ) -> OfflineCacheResultItem:
        started_at = time.monotonic()
        started_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        attempt = None
        attempt_root: Path | None = None
        with self._lock:
            self._update_progress_item(
                job,
                target,
                status="running",
                step="正在启动子进程",
                elapsed_seconds=0.0,
                started_monotonic=started_at,
            )
            self._sync_running_count(job)
        try:
            article_link = self._validate_article_short_link(target.article_link)
            article_directory = self._resolve_article_directory(target.archive_dir)
            attempt_root = self._attempt_root(job.job_id, target.id, article_directory)
            stage_dir = attempt_root / "stage"
            backup_dir = attempt_root / "backup"
            payload = {
                "article_id": target.id,
                "article_title": target.article_title,
                "article_link": article_link,
                "stage_dir": str(stage_dir),
                "browser_cache_dir": str(self.browser_cache_dir),
                "max_scroll_seconds": self.max_scroll_seconds,
                "resource_timeout_seconds": self.resource_timeout_seconds,
            }
            # 启动和登记必须处于同一临界区，确保 shutdown 能看到所有已启动子进程。
            with self._lock:
                if self._closed:
                    raise RuntimeError("离线缓存任务服务已关闭，未启动子进程。")
                attempt = self._process_control.start(
                    task_id=job.job_id,
                    attempt_id=f"article-{target.id}",
                    payload=payload,
                )
                self._active_attempts.add(attempt)
                self._update_progress_item(
                    job,
                    target,
                    status="running",
                    step="等待子进程 READY",
                    elapsed_seconds=time.monotonic() - started_at,
                )
            attempt.wait_ready(timeout_seconds=10.0)
            raw_result = attempt.wait_result(
                timeout_seconds=max(30.0, self.max_scroll_seconds + 60.0),
                on_progress=lambda event: self._record_progress(job, target, event),
            )
            if not bool(raw_result.get("ok")):
                raise OfflineCacheProcessError(
                    str(raw_result.get("message") or "离线缓存失败"),
                    result=raw_result,
                )
            if (
                not (stage_dir / "index.html").is_file()
                or not (stage_dir / "assets").is_dir()
            ):
                raise RuntimeError("子进程未生成完整的 index.html 和 assets")
            from src.services.archive.reader_resources import load_json
            from src.services.archive.resource_state_service import audit_article
            from src.services.archive.offline_completeness import offline_cache_state

            evidence = load_json(stage_dir / "assets/awa-manifest.json")
            _, old_state = audit_article(
                self.database_path, self.storage_root, target.id
            )
            cache_state = offline_cache_state(stage_dir, evidence)
            cache_complete = cache_state[0] == "complete"
            if (
                not cache_complete
                and old_state["offline"]["artifact_status"] == "complete"
            ):
                raise RuntimeError(
                    "本次网页缓存不完整，已保留现有可用缓存：" + cache_state[3]
                )
            resource_manifest = ResourceManifestBuilder().build(
                article_directory,
                planned_paths=("index.html", "assets"),
            )
            finished_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elapsed = time.monotonic() - started_at

            def database_operation(connection):
                ArticleRepository(connection).update_resource_manifest(
                    target.id,
                    resource_manifest,
                    collected_time=finished_time,
                )
                return FetchHistoryRepository(connection).append(
                    FetchHistoryWrite(
                        article_id=target.id,
                        account_id=target.account_id,
                        target_account_name=target.account_name,
                        target_title=target.article_title,
                        target_link=target.article_link,
                        task_type="offline_cache",
                        resource_manifest=resource_manifest,
                        status=TaskStatus.SUCCESS
                        if cache_complete
                        else TaskStatus.FAILED,
                        started_time=started_time,
                        finished_time=finished_time,
                        duration_seconds=elapsed,
                        error_stage="" if cache_complete else "offline_completeness",
                        error_message="" if cache_complete else cache_state[3],
                        output_dir=target.archive_dir,
                    )
                )

            self._resource_commit.commit(
                database_path=self.database_path,
                stage_root=stage_dir,
                target_root=article_directory,
                backup_root=backup_dir,
                resource_paths=("index.html", "assets"),
                database_operation=database_operation,
            )
            from src.services.archive.resource_state_service import (
                audit_article,
                set_attempt,
            )

            key, state = audit_article(self.database_path, self.storage_root, target.id)
            set_attempt(
                self.database_path,
                key,
                ["offline"],
                state["offline"]["artifact_status"],
                state["offline"]["reason"],
            )
            return OfflineCacheResultItem(
                article_id=target.id,
                article_title=target.article_title,
                ok=cache_complete,
                status="done" if cache_complete else "partial",
                message="网页缓存完成" if cache_complete else cache_state[3],
                archive_dir=target.archive_dir,
                index_html_path=str(article_directory / "index.html"),
                resource_count=int(raw_result.get("resource_count") or 0),
                warning=str(raw_result.get("warning") or ""),
                elapsed_seconds=elapsed,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started_at
            message = self._failure_message(exc)
            self._append_failure_history(
                target=target,
                started_time=started_time,
                elapsed_seconds=elapsed,
                message=message,
            )
            return OfflineCacheResultItem(
                article_id=target.id,
                article_title=target.article_title,
                ok=False,
                status="failed",
                message=message,
                archive_dir=target.archive_dir,
                elapsed_seconds=elapsed,
            )
        finally:
            if attempt is not None:
                with self._lock:
                    self._active_attempts.discard(attempt)
            if attempt_root is not None and attempt_root.exists():
                shutil.rmtree(attempt_root, ignore_errors=True)

    def _append_failure_history(
        self,
        *,
        target: OfflineCacheArticleRecord,
        started_time: str,
        elapsed_seconds: float,
        message: str,
    ) -> None:
        finished_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            from src.services.archive.resource_state_service import (
                audit_article,
                set_attempt,
            )

            key, _ = audit_article(self.database_path, self.storage_root, target.id)
            set_attempt(self.database_path, key, ["offline"], "failed", message)
            with self._write_coordinator.hold():
                with sqlite_connection(self.database_path) as connection:
                    FetchHistoryRepository(connection).append(
                        FetchHistoryWrite.failed(
                            article_id=target.id,
                            account_id=target.account_id,
                            target_account_name=target.account_name,
                            target_title=target.article_title,
                            target_link=target.article_link,
                            task_type="offline_cache",
                            started_time=started_time,
                            finished_time=finished_time,
                            duration_seconds=elapsed_seconds,
                            error_stage="offline_cache",
                            error_message=message,
                        )
                    )
        except Exception:
            pass

    def _resolve_article_directory(self, archive_dir: str) -> Path:
        relative = Path(str(archive_dir).strip())
        if not archive_dir.strip() or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("文章归档目录无效")
        resolved = (self.storage_root / relative).resolve()
        if not resolved.is_relative_to(self.storage_root):
            raise ValueError("文章归档目录超出 storages")
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    @staticmethod
    def _validate_article_short_link(article_link: str) -> str:
        normalized = str(article_link or "").strip()
        if WECHAT_SHORT_LINK_PATTERN.fullmatch(normalized) is None:
            raise ValueError(
                "文章缺少有效的微信文章短链：https://mp.weixin.qq.com/s/<slug>"
            )
        return normalized

    def _attempt_root(
        self, job_id: str, article_id: int, article_directory: Path
    ) -> Path:
        configured = self.temp_root / "offline-cache" / job_id / str(article_id)
        if configured.anchor.casefold() == article_directory.anchor.casefold():
            return configured
        return (
            article_directory.parent / ".awa-offline-cache" / job_id / str(article_id)
        )

    def _record_progress(
        self,
        job: OfflineCacheJob,
        target: OfflineCacheArticleRecord,
        event: dict[str, Any],
    ) -> None:
        with self._lock:
            current = job.progress_items.get(target.id)
            elapsed = float(event.get("elapsed_seconds") or 0.0)
            if current is not None and current.started_monotonic > 0:
                elapsed = max(elapsed, time.monotonic() - current.started_monotonic)
            step = str(event.get("name") or "正在缓存")
            self._update_progress_item(
                job,
                target,
                status="running",
                step=step,
                elapsed_seconds=elapsed,
            )
            job.message = f"{target.article_title}：{step}"

    @staticmethod
    def _update_progress_item(
        job: OfflineCacheJob,
        target: OfflineCacheArticleRecord,
        *,
        status: str,
        step: str,
        elapsed_seconds: float,
        started_monotonic: float | None = None,
    ) -> None:
        current = job.progress_items.get(target.id)
        job.progress_items[target.id] = OfflineCacheProgressItem(
            article_id=target.id,
            article_title=target.article_title,
            status=status,
            step=step,
            elapsed_seconds=max(0.0, float(elapsed_seconds)),
            started_monotonic=(
                float(started_monotonic)
                if started_monotonic is not None
                else (current.started_monotonic if current is not None else 0.0)
            ),
        )

    @staticmethod
    def _sync_running_count(job: OfflineCacheJob) -> None:
        job.running = sum(
            1 for item in job.progress_items.values() if item.status == "running"
        )

    @staticmethod
    def _failure_message(exc: Exception) -> str:
        if isinstance(exc, OfflineCacheProcessError) and exc.result:
            return str(exc.result.get("message") or exc)
        return str(exc) or type(exc).__name__

    @staticmethod
    def _empty_job_message(skipped: int) -> str:
        if skipped:
            return f"没有需要新增缓存的文章，已跳过 {skipped} 篇已有缓存。"
        return "没有可缓存的文章。"

    @staticmethod
    def _completion_message(job: OfflineCacheJob, failed: int) -> str:
        succeeded = job.finished - failed
        parts = [f"新增缓存 {succeeded} 篇"]
        if job.skipped:
            parts.append(f"跳过已有缓存 {job.skipped} 篇")
        if failed:
            parts.append(f"失败 {failed} 篇")
        return "，".join(parts) + "。"


__all__ = [
    "OfflineCacheConflictError",
    "OfflineCacheJob",
    "OfflineCacheJobService",
    "OfflineCacheProgressItem",
    "OfflineCacheResultItem",
    "has_complete_offline_cache",
]
