from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Protocol

from src.domain.enums import ErrorCode, TaskStatus
from src.domain.models import ArticleTarget, TaskCommand, TaskContext
from src.domain.results import ServiceResult, TaskResult
from src.modules.window.wechat_home_reader import WechatHomeInfo
from src.modules.window.window_models import WindowInfo
from src.services.capture.attempt_history_service import AttemptHistoryService
from src.services.capture.attempt_policy import AttemptPolicy
from src.services.capture.html_parse_save_service import ArticleSaveData
from src.services.capture.single_article_capture_service import (
    SingleArticleCaptureData,
    SingleCaptureSettings,
)


@dataclass(frozen=True, slots=True)
class ArticleCaptureSummary:
    requested_success_count: int
    success_count: int
    failed_attempt_count: int
    skipped_count: int
    total_attempts: int
    failure_messages: tuple[str, ...]
    saved_articles: tuple[ArticleSaveData, ...]
    comment_success_count: int
    comment_failed_count: int
    comment_skipped_count: int
    comment_count: int
    reply_count: int


@dataclass(slots=True)
class _CommentTotals:
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    comment_count: int = 0
    reply_count: int = 0


class Preflight(Protocol):
    def run(self, context: TaskContext) -> ServiceResult[Any]: ...


class HomeReader(Protocol):
    def read(self, home_window: WindowInfo) -> WechatHomeInfo: ...


class ArticleCursor(Protocol):
    def next_candidate(self, home_window: WindowInfo) -> ArticleTarget | None: ...

    def mark_processed(self, target: ArticleTarget) -> None: ...


class ArticleCaptureService:
    """维护全局尝试预算，并把窗口捕获、解析保存和历史串成完整主链。"""

    def __init__(
        self,
        *,
        preflight: Preflight,
        home_finder: Callable[[], WindowInfo | None],
        home_reader: HomeReader,
        cursor_factory: Callable[[WindowInfo, str], ArticleCursor],
        single_capture: Any,
        html_save: Any,
        comment_collect: Any | None = None,
        comment_job_manager_factory: Callable[[], Any] | None = None,
        collected_lookup: Any | None = None,
        history_factory: Callable[[Any], Any] = lambda path: AttemptHistoryService(database_path=path),
        cleanup: Callable[[TaskContext, WindowInfo | None], None] = lambda _context, _home: None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._preflight = preflight
        self._home_finder = home_finder
        self._home_reader = home_reader
        self._cursor_factory = cursor_factory
        self._single_capture = single_capture
        self._html_save = html_save
        self._comment_collect = comment_collect
        self._comment_job_manager_factory = comment_job_manager_factory
        self._collected_lookup = collected_lookup
        self._history_factory = history_factory
        self._cleanup = cleanup
        self._sleep = sleep
        self._monotonic = monotonic

    def run(
        self,
        command: TaskCommand,
        context: TaskContext,
        *,
        single_capture_settings: SingleCaptureSettings,
        request_timeout_seconds: float,
        comment_timeout_seconds: float | None = None,
        comment_page_interval_seconds: float = 0,
        comment_max_pages: int = 5,
        runtime_state: Any | None = None,
    ) -> TaskResult[ArticleCaptureSummary]:
        home_window: WindowInfo | None = None
        warnings: list[str] = []
        failure_messages: list[str] = []
        saved_articles: list[ArticleSaveData] = []
        comment_totals = _CommentTotals()
        failed_attempts = 0
        processed_targets = 0
        skipped = 0
        policy = AttemptPolicy(
            max_attempts=command.max_attempts,
            article_retry_count=command.article_retry_count,
        )
        traverse_all = command.target_success_count == 0
        history = self._history_factory(context.db_path)
        final_status = TaskStatus.FAILED
        final_error = ErrorCode.INTERNAL_ERROR
        final_message = "文章采集未完成"
        comment_manager: Any | None = None
        comment_manager_drained = False
        _runtime_call(
            runtime_state,
            "set_total_label",
            "全部" if command.target_success_count == 0 else str(command.target_success_count),
        )
        _runtime_action(runtime_state, "准备采集任务")
        try:
            preflight = self._preflight.run(context)
            if preflight.status is not TaskStatus.SUCCESS:
                _runtime_error(runtime_state, preflight.message)
                return self._result(
                    status=TaskStatus.FAILED,
                    error_code=preflight.error_code or ErrorCode.PREFLIGHT_FAILED,
                    message=preflight.message,
                    command=command,
                    policy=policy,
                    failed_attempts=failed_attempts,
                    skipped=skipped,
                    failures=failure_messages,
                    saved=saved_articles,
                    warnings=warnings,
                )
            _runtime_action(runtime_state, "定位公众号主页")
            home_window = self._home_finder()
            if home_window is None:
                _runtime_error(runtime_state, "未找到可读的微信公众号主页窗口")
                return self._result(
                    status=TaskStatus.FAILED,
                    error_code=ErrorCode.WINDOW_NOT_FOUND,
                    message="未找到可读的微信公众号主页窗口",
                    command=command,
                    policy=policy,
                    failed_attempts=failed_attempts,
                    skipped=skipped,
                    failures=failure_messages,
                    saved=saved_articles,
                    warnings=warnings,
                )
            _runtime_action(runtime_state, "读取公众号名称")
            home_info = self._home_reader.read(home_window)
            _runtime_call(runtime_state, "set_account_name", home_info.account_name)
            cursor = self._cursor_factory(home_window, home_info.account_name)

            while traverse_all or processed_targets < command.target_success_count:
                if _is_cancelled(context.cancel_token):
                    final_status = TaskStatus.CANCELLED
                    final_error = ErrorCode.CANCELLED
                    final_message = "文章采集已取消"
                    break
                _runtime_action(runtime_state, "识别文章卡片")
                target = cursor.next_candidate(home_window)
                if target is None:
                    if skipped > 0 and processed_targets == 0:
                        final_status = TaskStatus.SUCCESS
                        final_message = (
                            "当前主页没有更多未采集文章，"
                            f"已跳过 {skipped} 篇已采集记录"
                        )
                    elif traverse_all:
                        final_status = TaskStatus.SUCCESS
                        final_message = "已遍历当前主页全部可见文章"
                    else:
                        final_message = "当前主页没有更多可采集文章"
                    break
                if policy.global_exhausted:
                    final_message = "已达到全局最大尝试次数"
                    break

                _runtime_action(runtime_state, "选择待采集文章")
                _runtime_call(runtime_state, "set_task_info", target.title)
                if command.skip_collected_records:
                    _runtime_action(runtime_state, "检查已采集记录")
                    try:
                        if self._collected_lookup is None:
                            raise RuntimeError("已采集记录查询服务未配置")
                        already_collected = self._collected_lookup.is_collected(
                            database_path=context.db_path,
                            account_name=home_info.account_name,
                            article_title=target.title,
                            storage_root=context.storage_root,
                            required_resources=tuple(['detail'] + (['comments','avatars'] if command.collect_comments else []) + (['offline'] if command.build_offline_cache else [])),
                        )
                    except Exception as exc:
                        message = (
                            "已采集记录查询失败："
                            f"{type(exc).__name__}: {exc}"
                        )
                        _runtime_error(runtime_state, message)
                        return self._result(
                            status=TaskStatus.FAILED,
                            error_code=ErrorCode.DB_UNAVAILABLE,
                            message=message,
                            command=command,
                            policy=policy,
                            failed_attempts=failed_attempts,
                            skipped=skipped,
                            failures=failure_messages,
                            saved=saved_articles,
                            warnings=warnings,
                            comments=comment_totals,
                        )
                    if already_collected:
                        skipped += 1
                        cursor.mark_processed(target)
                        _runtime_action(runtime_state, "跳过已采集文章")
                        continue

                target_succeeded = False
                target_runtime_started = False
                target_runtime_finished = False
                target_runtime_deferred = False
                target_monotonic = self._monotonic()
                _runtime_call(
                    runtime_state,
                    "start_article",
                    article_key=target.fingerprint,
                    task_info=target.title,
                    collect_comments=command.collect_comments,
                )
                target_runtime_started = True
                while policy.can_attempt(target.fingerprint):
                    if _is_cancelled(context.cancel_token):
                        final_status = TaskStatus.CANCELLED
                        final_error = ErrorCode.CANCELLED
                        final_message = "文章采集已取消"
                        break
                    ticket = policy.begin(target.fingerprint)
                    attempt_monotonic = self._monotonic()
                    capture = self._single_capture.capture_once(
                        context=context,
                        attempt_id=ticket.attempt_id,
                        home_window=home_window,
                        target=target,
                        settings=single_capture_settings,
                        runtime_state=runtime_state,
                    )
                    if capture.status is not TaskStatus.SUCCESS or capture.data is None:
                        failed_attempts += 1
                        message = capture.message or "单次文章捕获失败"
                        failure_messages.append(message)
                        _runtime_article_stage(
                            runtime_state,
                            article_key=target.fingerprint,
                            stage="window",
                            label="点击与关闭",
                            status="failed",
                            duration_seconds=capture.duration_seconds,
                            message=message,
                        )
                        _runtime_article_error(runtime_state, target.fingerprint, message)
                        self._record_failure_safely(
                            history=history,
                            warnings=warnings,
                            ticket=ticket,
                            target=target,
                            duration_seconds=max(capture.duration_seconds, self._monotonic() - attempt_monotonic),
                            error_stage=(capture.error_code or ErrorCode.INTERNAL_ERROR).value,
                            error_message=message,
                        )
                        # 本次运行内只要文章已经进入点击/捕获尝试，就算处理过；
                        # 子进程获取失败只记录失败，不再重复点击同一篇文章。
                        break
                    else:
                        _runtime_action(runtime_state, "获取文章内容")
                        _runtime_action(runtime_state, "解析文章内容")
                        save_step_started = time.monotonic()
                        save = self._html_save.save(
                            context=context,
                            target=target,
                            capture_result=capture.data.capture_result,
                            attempt_started_at=ticket.started_at,
                            duration_seconds=max(
                                capture.duration_seconds,
                                self._monotonic() - attempt_monotonic,
                            ),
                            request_timeout_seconds=request_timeout_seconds,
                        )
                        save_step_duration = max(0.0, time.monotonic() - save_step_started)
                        if save.status is TaskStatus.SUCCESS and save.data is not None:
                            history.mark_success(ticket.attempt_id, history_id=save.data.history_id)
                            saved_articles.append(save.data)
                            _runtime_action(runtime_state, "保存初始内容")
                            _runtime_article_stage(
                                runtime_state,
                                article_key=target.fingerprint,
                                stage="html",
                                label="HTML 解析与存储",
                                status="success",
                                duration_seconds=save_step_duration,
                                message="初始内容已保存",
                            )
                            if command.collect_comments:
                                if self._comment_job_manager_factory is not None:
                                    try:
                                        if comment_manager is None:
                                            comment_manager = self._comment_job_manager_factory()
                                        comment_manager.submit(
                                            context=context,
                                            article=save.data,
                                            article_key=target.fingerprint,
                                            article_title=target.title,
                                            article_started_monotonic=target_monotonic,
                                            timeout_seconds=(
                                                request_timeout_seconds
                                                if comment_timeout_seconds is None
                                                else comment_timeout_seconds
                                            ),
                                            page_interval_seconds=comment_page_interval_seconds,
                                            max_pages=comment_max_pages,
                                            runtime_state=runtime_state,
                                        )
                                        target_runtime_deferred = True
                                    except Exception as exc:
                                        comment_totals.failed_count += 1
                                        message = self._comment_message(
                                            save.data,
                                            f"提交失败 {type(exc).__name__}: {exc}",
                                        )
                                        warnings.append(message)
                                        _runtime_article_stage(
                                            runtime_state,
                                            article_key=target.fingerprint,
                                            stage="comment",
                                            label="评论采集",
                                            status="failed",
                                            duration_seconds=0.0,
                                            message=message,
                                        )
                                        _runtime_article_error(
                                            runtime_state,
                                            target.fingerprint,
                                            message,
                                        )
                                        _runtime_call(
                                            runtime_state,
                                            "finish_article",
                                            article_key=target.fingerprint,
                                            duration_seconds=self._monotonic() - target_monotonic,
                                            count_for_average=True,
                                            status="failed",
                                        )
                                        target_runtime_finished = True
                                else:
                                    _runtime_action(runtime_state, "采集评论")
                                    comment_ok = self._collect_comments_after_save(
                                        context=context,
                                        article=save.data,
                                        article_key=target.fingerprint,
                                        timeout_seconds=(
                                            request_timeout_seconds
                                            if comment_timeout_seconds is None
                                            else comment_timeout_seconds
                                        ),
                                        page_interval_seconds=comment_page_interval_seconds,
                                        max_pages=comment_max_pages,
                                        totals=comment_totals,
                                        warnings=warnings,
                                        runtime_state=runtime_state,
                                    )
                                    _runtime_call(
                                        runtime_state,
                                        "finish_article",
                                        article_key=target.fingerprint,
                                        duration_seconds=self._monotonic() - target_monotonic,
                                        count_for_average=True,
                                        status="success" if comment_ok else "failed",
                                    )
                                    target_runtime_finished = True
                            else:
                                _runtime_call(
                                    runtime_state,
                                    "finish_article",
                                    article_key=target.fingerprint,
                                    duration_seconds=self._monotonic() - target_monotonic,
                                    count_for_average=True,
                                    status="success",
                                )
                                target_runtime_finished = True
                            target_succeeded = True
                            break
                        failed_attempts += 1
                        message = save.message or "文章解析保存失败"
                        failure_messages.append(message)
                        _runtime_article_stage(
                            runtime_state,
                            article_key=target.fingerprint,
                            stage="html",
                            label="HTML 解析与存储",
                            status="failed",
                            duration_seconds=save_step_duration,
                            message=message,
                        )
                        _runtime_article_error(runtime_state, target.fingerprint, message)
                        self._record_failure_safely(
                            history=history,
                            warnings=warnings,
                            ticket=ticket,
                            target=target,
                            duration_seconds=max(save.duration_seconds, self._monotonic() - attempt_monotonic),
                            error_stage=(save.error_code or ErrorCode.SAVE_FAILED).value,
                            error_message=message,
                        )
                        # 捕获已完成但解析/保存失败时，同样不重试点击当前文章。
                        break
                    if policy.can_attempt(target.fingerprint) and command.request_interval_seconds:
                        self._sleep(command.request_interval_seconds)

                cursor.mark_processed(target)
                processed_targets += 1
                if (
                    target_runtime_started
                    and not target_runtime_finished
                    and not target_runtime_deferred
                ):
                    _runtime_call(
                        runtime_state,
                        "finish_article",
                        article_key=target.fingerprint,
                        duration_seconds=0.0,
                        count_for_average=False,
                        status="failed",
                    )
                if final_status is TaskStatus.CANCELLED:
                    break
                if (
                    not traverse_all
                    and target_succeeded
                    and processed_targets >= command.target_success_count
                ):
                    final_status = TaskStatus.SUCCESS
                    final_error = ErrorCode.INTERNAL_ERROR
                    final_message = "已达到目标处理数量"
                    break

            if final_status is not TaskStatus.CANCELLED:
                if (
                    final_status is TaskStatus.SUCCESS
                    and processed_targets == 0
                    and skipped > 0
                ):
                    pass
                elif traverse_all and final_status is TaskStatus.SUCCESS:
                    pass
                elif not traverse_all and processed_targets >= command.target_success_count:
                    final_status = TaskStatus.SUCCESS if saved_articles else TaskStatus.FAILED
                    final_error = ErrorCode.INTERNAL_ERROR
                    final_message = "文章采集完成" if saved_articles else "目标文章均未成功保存"
                else:
                    final_status = TaskStatus.FAILED
                    final_error = ErrorCode.CAPTURE_EMPTY
                    if policy.global_exhausted:
                        final_message = "已达到全局最大尝试次数"
            if comment_manager is not None:
                if _is_cancelled(context.cancel_token):
                    comment_manager.cancel()
                else:
                    _runtime_action(runtime_state, "等待评论子进程完成")
                outcomes = comment_manager.drain()
                comment_manager_drained = True
                self._merge_comment_outcomes(
                    outcomes=outcomes,
                    totals=comment_totals,
                    warnings=warnings,
                    runtime_state=runtime_state,
                )
            return self._result(
                status=final_status,
                error_code=None if final_status is TaskStatus.SUCCESS else final_error,
                message=final_message,
                command=command,
                policy=policy,
                failed_attempts=failed_attempts,
                skipped=skipped,
                failures=failure_messages,
                saved=saved_articles,
                warnings=warnings,
                comments=comment_totals,
            )
        except Exception as exc:
            if comment_manager is not None and not comment_manager_drained:
                try:
                    comment_manager.cancel()
                    outcomes = comment_manager.drain()
                    comment_manager_drained = True
                    self._merge_comment_outcomes(
                        outcomes=outcomes,
                        totals=comment_totals,
                        warnings=warnings,
                        runtime_state=runtime_state,
                    )
                except Exception:
                    pass
            _runtime_error(runtime_state, f"{type(exc).__name__}: {exc}")
            return self._result(
                status=TaskStatus.FAILED,
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"文章采集主流程异常：{type(exc).__name__}: {exc}",
                command=command,
                policy=policy,
                failed_attempts=failed_attempts,
                skipped=skipped,
                failures=failure_messages,
                saved=saved_articles,
                warnings=warnings,
                comments=comment_totals,
            )
        finally:
            if comment_manager is not None and not comment_manager_drained:
                try:
                    comment_manager.cancel()
                    comment_manager.drain()
                except Exception:
                    pass
            _runtime_action(runtime_state, "清理运行资源")
            try:
                self._cleanup(context, home_window)
            except Exception:
                pass

    @staticmethod
    def _comment_message(article: ArticleSaveData, message: str) -> str:
        return f"文章 {article.article_id} 评论任务：{message}"

    def _collect_comments_after_save(
        self,
        *,
        context: TaskContext,
        article: ArticleSaveData,
        article_key: str,
        timeout_seconds: float,
        page_interval_seconds: float,
        max_pages: int,
        totals: _CommentTotals,
        warnings: list[str],
        runtime_state: Any | None = None,
    ) -> bool:
        if self._comment_collect is None:
            totals.failed_count += 1
            message = self._comment_message(article, "评论服务未配置")
            warnings.append(message)
            _runtime_article_error(runtime_state, article_key, message)
            return False
        try:
            result = self._comment_collect.collect(
                context=context,
                article=article,
                timeout_seconds=timeout_seconds,
                page_interval_seconds=page_interval_seconds,
                max_pages=max_pages,
            )
        except Exception as exc:
            totals.failed_count += 1
            message = self._comment_message(article, f"异常 {type(exc).__name__}")
            warnings.append(message)
            _runtime_article_error(runtime_state, article_key, message)
            return False
        if result.status is TaskStatus.SUCCESS and result.data is not None:
            totals.success_count += 1
            totals.comment_count += max(0, int(result.data.comment_count))
            totals.reply_count += max(0, int(result.data.reply_count))
            _runtime_article_stage(
                runtime_state,
                article_key=article_key,
                stage="comment",
                label="评论采集",
                status="success",
                duration_seconds=result.duration_seconds,
                message=result.message or "评论采集完成",
            )
            return True
        elif result.status is TaskStatus.SKIPPED:
            totals.skipped_count += 1
            warnings.append(self._comment_message(article, result.message or "已跳过"))
            _runtime_article_stage(
                runtime_state,
                article_key=article_key,
                stage="comment",
                label="评论采集",
                status="failed",
                duration_seconds=result.duration_seconds,
                message=result.message or "评论采集已跳过",
            )
            _runtime_article_error(runtime_state, article_key, result.message or "评论采集已跳过")
            return False
        else:
            totals.failed_count += 1
            message = self._comment_message(article, result.message or "获取失败")
            warnings.append(message)
            _runtime_article_stage(
                runtime_state,
                article_key=article_key,
                stage="comment",
                label="评论采集",
                status="failed",
                duration_seconds=result.duration_seconds,
                message=message,
            )
            _runtime_article_error(runtime_state, article_key, message)
            return False

    @staticmethod
    def _merge_comment_outcomes(
        *,
        outcomes: Any,
        totals: _CommentTotals,
        warnings: list[str],
        runtime_state: Any | None,
    ) -> None:
        for outcome in outcomes:
            status = getattr(outcome, "status", TaskStatus.FAILED)
            if status is TaskStatus.SUCCESS:
                totals.success_count += 1
                totals.comment_count += max(0, int(getattr(outcome, "comment_count", 0)))
                totals.reply_count += max(0, int(getattr(outcome, "reply_count", 0)))
            elif status is TaskStatus.SKIPPED:
                totals.skipped_count += 1
                warnings.append(str(getattr(outcome, "message", "评论采集已跳过")))
            else:
                totals.failed_count += 1
                message = str(getattr(outcome, "message", "评论采集失败"))
                warnings.append(message)
                if not str(getattr(outcome, "article_key", "")):
                    _runtime_error(runtime_state, message)

    @staticmethod
    def _record_failure_safely(**kwargs: Any) -> None:
        history = kwargs.pop("history")
        warnings = kwargs.pop("warnings")
        ticket = kwargs.pop("ticket")
        try:
            history.record_failure(
                attempt_id=ticket.attempt_id,
                started_at=ticket.started_at,
                **kwargs,
            )
        except Exception as exc:
            warnings.append(f"attempt {ticket.attempt_id} 失败历史未落库：{type(exc).__name__}")

    @staticmethod
    def _result(
        *,
        status: TaskStatus,
        error_code: ErrorCode | None,
        message: str,
        command: TaskCommand,
        policy: AttemptPolicy,
        failed_attempts: int,
        skipped: int,
        failures: list[str],
        saved: list[ArticleSaveData],
        warnings: list[str],
        comments: _CommentTotals | None = None,
    ) -> TaskResult[ArticleCaptureSummary]:
        comment_totals = comments or _CommentTotals()
        return TaskResult(
            status=status,
            data=ArticleCaptureSummary(
                requested_success_count=command.target_success_count,
                success_count=len(saved),
                failed_attempt_count=failed_attempts,
                skipped_count=skipped,
                total_attempts=policy.total_attempts,
                failure_messages=tuple(failures),
                saved_articles=tuple(saved),
                comment_success_count=comment_totals.success_count,
                comment_failed_count=comment_totals.failed_count,
                comment_skipped_count=comment_totals.skipped_count,
                comment_count=comment_totals.comment_count,
                reply_count=comment_totals.reply_count,
            ),
            error_code=error_code,
            message=message,
            warnings=tuple(warnings),
        )


def _is_cancelled(token: Any) -> bool:
    if token is None:
        return False
    for name in ("is_set", "is_cancelled", "cancelled"):
        value = getattr(token, name, None)
        try:
            return bool(value()) if callable(value) else bool(value)
        except Exception:
            continue
    return False


def _runtime_action(runtime_state: Any | None, action: str) -> None:
    _runtime_call(runtime_state, "set_action", action)


def _runtime_error(runtime_state: Any | None, message: str) -> None:
    _runtime_call(runtime_state, "record_error", message)


def _runtime_article_error(
    runtime_state: Any | None,
    article_key: str,
    message: str,
) -> None:
    _runtime_call(runtime_state, "record_article_error", article_key, message)


def _runtime_article_stage(
    runtime_state: Any | None,
    *,
    article_key: str,
    stage: str,
    label: str,
    status: str,
    duration_seconds: float,
    message: str,
) -> None:
    _runtime_call(
        runtime_state,
        "record_article_stage",
        article_key=article_key,
        stage=stage,
        label=label,
        status=status,
        duration_seconds=max(0.0, float(duration_seconds)),
        message=message,
    )


def _runtime_call(runtime_state: Any | None, method_name: str, *args: Any, **kwargs: Any) -> None:
    method = getattr(runtime_state, method_name, None)
    if callable(method):
        try:
            method(*args, **kwargs)
        except Exception:
            pass
