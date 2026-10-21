from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
from typing import Callable, Protocol

from src.domain.enums import ErrorCode, TaskStatus
from src.domain.models import (
    ArticleTarget,
    MitmCaptureResult,
    ResourceManifest,
    TaskContext,
)
from src.domain.results import ServiceResult
from src.modules.archive.archive_path_builder import ArchivePathBuilder
from src.modules.archive.resource_manifest_builder import ResourceManifestBuilder
from src.modules.request.article_html_requester import (
    ArticleHtmlRequestError,
    ArticleHtmlRequester,
    PreparedArticleHtml,
)
from src.modules.request.article_parser import ArticleParseError, WechatArticleParser
from src.services.archive.resource_commit_service import ResourceCommitService
from src.services.runtime.database_write_coordinator import DatabaseWriteCoordinator
from src.storage.repositories.account_repository import AccountRepository
from src.storage.repositories.article_repository import ArticleIndexWrite, ArticleRepository
from src.storage.repositories.fetch_history_repository import (
    FetchHistoryRepository,
    FetchHistoryWrite,
)
from src.storage.sqlite.connection import sqlite_connection


MAIN_RESOURCE_PATHS = (
    Path("origin/main.html"),
    Path("origin/request.json"),
    Path("article_detail.json"),
)


class HtmlRequester(Protocol):
    def prepare(
        self,
        capture: MitmCaptureResult,
        *,
        timeout_seconds: float,
    ) -> PreparedArticleHtml: ...


@dataclass(frozen=True, slots=True)
class ArticleSaveData:
    article_id: int
    account_id: int
    history_id: int
    article_directory: Path
    archive_dir: str
    detail_path: Path
    resource_manifest: ResourceManifest
    html_source: str
    attempt_id: str


class HtmlParseSaveService:
    """准备、解析、校验并以文件补偿 + SQLite 事务保存一篇文章。"""

    def __init__(
        self,
        *,
        requester: HtmlRequester | None = None,
        parser: WechatArticleParser | None = None,
        manifest_builder: ResourceManifestBuilder | None = None,
        commit_service: ResourceCommitService | None = None,
        write_coordinator: DatabaseWriteCoordinator | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._requester = requester or ArticleHtmlRequester()
        self._parser = parser or WechatArticleParser()
        self._manifest_builder = manifest_builder or ResourceManifestBuilder()
        self._commit_service = commit_service or ResourceCommitService(
            write_coordinator=write_coordinator,
        )
        self._now = now

    def save(
        self,
        *,
        context: TaskContext,
        target: ArticleTarget,
        capture_result: MitmCaptureResult,
        attempt_started_at: datetime,
        duration_seconds: float,
        request_timeout_seconds: float,
    ) -> ServiceResult[ArticleSaveData]:
        try:
            prepared = self._requester.prepare(
                capture_result,
                timeout_seconds=request_timeout_seconds,
            )
        except ArticleHtmlRequestError as exc:
            return ServiceResult.failure(ErrorCode.REFERENCE_REQUEST_FAILED, str(exc))

        fallback_link = _capture_reference_url(capture_result)
        try:
            detail = self._parser.parse_and_validate(
                prepared.html,
                fallback_link=fallback_link,
            )
        except ArticleParseError as exc:
            return ServiceResult.failure(ErrorCode.PARSE_FAILED, str(exc))

        expected_link = getattr(target,'expected_article_link','')
        if expected_link and (detail.article_short_link or detail.article_link) != expected_link:
            return ServiceResult.failure(ErrorCode.PARSE_FAILED,'打开的文章链接与指定补采目标不一致，未覆盖任何旧文件')

        collected_at = self._now()
        collected_text = _format_time(collected_at)
        safe_duration = max(0.0, float(duration_seconds))
        # SQLite 的 article_link 字段用于文章唯一身份；业务上使用微信短链作为稳定身份。
        sqlite_article_link = detail.article_short_link or detail.article_link
        try:
            existing_archive_dir = self._find_existing_archive_dir(
                context=context,
                account_name=detail.account_name,
                article_link=sqlite_article_link,
            )
            archive_path = ArchivePathBuilder(context.storage_root).build(
                account_name=detail.account_name,
                article_title=detail.article_title,
                published_article_time=detail.published_article_time,
                article_link=sqlite_article_link,
                existing_archive_dir=existing_archive_dir,
            )
            stage_root = (
                context.temp_dir
                / "article_stage"
                / archive_path.article_key
                / capture_result.attempt_id
            )
            backup_root = (
                context.temp_dir
                / "article_backup"
                / archive_path.article_key
                / capture_result.attempt_id
            )
            _prepare_empty_stage(stage_root)
            _write_stage_files(
                stage_root=stage_root,
                prepared=prepared,
                detail_mapping={
                    **asdict(detail),
                    "html_source": prepared.html_source,
                    "duration_seconds": safe_duration,
                    "collect_time": collected_text,
                    "record_status": "saved",
                },
                capture_result=capture_result,
            )
            manifest = self._manifest_builder.build(
                archive_path.article_directory,
                planned_paths=MAIN_RESOURCE_PATHS,
            )

            def database_operation(connection):
                account_id = AccountRepository(connection).upsert(
                    detail.account_name,
                    now=collected_text,
                )
                article_id = ArticleRepository(connection).upsert(
                    ArticleIndexWrite(
                        account_id=account_id,
                        article_title=detail.article_title,
                        published_article_time=detail.published_article_time,
                        article_link=sqlite_article_link,
                        archive_dir=archive_path.relative_archive_dir,
                        resource_manifest=manifest,
                        collected_time=collected_text,
                    )
                )
                history_id = FetchHistoryRepository(connection).append(
                    FetchHistoryWrite(
                        article_id=article_id,
                        account_id=account_id,
                        target_account_name=target.account_name,
                        target_title=target.title,
                        target_link=sqlite_article_link,
                        task_type="article_capture",
                        resource_manifest=manifest,
                        status=TaskStatus.SUCCESS,
                        started_time=_format_time(attempt_started_at),
                        finished_time=collected_text,
                        duration_seconds=safe_duration,
                        output_dir=archive_path.relative_archive_dir,
                    )
                )
                return account_id, article_id, history_id

            account_id, article_id, history_id = self._commit_service.commit(
                database_path=context.db_path,
                stage_root=stage_root,
                target_root=archive_path.article_directory,
                backup_root=backup_root,
                resource_paths=MAIN_RESOURCE_PATHS,
                database_operation=database_operation,
            )
            return ServiceResult.success(
                ArticleSaveData(
                    article_id=article_id,
                    account_id=account_id,
                    history_id=history_id,
                    article_directory=archive_path.article_directory,
                    archive_dir=archive_path.relative_archive_dir,
                    detail_path=archive_path.article_directory / "article_detail.json",
                    resource_manifest=manifest,
                    html_source=prepared.html_source,
                    attempt_id=capture_result.attempt_id,
                ),
                duration_seconds=safe_duration,
            )
        except Exception as exc:
            return ServiceResult.failure(
                ErrorCode.SAVE_FAILED,
                f"文章资源保存失败：{type(exc).__name__}: {exc}",
                duration_seconds=safe_duration,
            )

    @staticmethod
    def _find_existing_archive_dir(
        *,
        context: TaskContext,
        account_name: str,
        article_link: str,
    ) -> str:
        with sqlite_connection(context.db_path, write=False) as connection:
            account = AccountRepository(connection).get_by_name(account_name)
            if account is None:
                return ""
            article = ArticleRepository(connection).get_by_account_and_link(
                account.id,
                article_link,
            )
            return "" if article is None else article.archive_dir


def _capture_reference_url(capture: MitmCaptureResult) -> str:
    if not capture.reference:
        return ""
    return str(capture.reference.get("url", "") or "")


def _prepare_empty_stage(stage_root: Path) -> None:
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=False)


def _write_stage_files(
    *,
    stage_root: Path,
    prepared: PreparedArticleHtml,
    detail_mapping: dict[str, object],
    capture_result: MitmCaptureResult,
) -> None:
    origin = stage_root / "origin"
    origin.mkdir(parents=True, exist_ok=True)
    (origin / "main.html").write_text(prepared.html, encoding="utf-8")
    request_evidence = {
        **prepared.request_evidence,
        "task_id": capture_result.task_id,
        "attempt_id": capture_result.attempt_id,
        "html_source": prepared.html_source,
    }
    (origin / "request.json").write_text(
        json.dumps(request_evidence, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (stage_root / "article_detail.json").write_text(
        json.dumps(detail_mapping, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")
