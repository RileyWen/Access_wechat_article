from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable

from src.services.archive.reader_service import ReaderService
from src.services.archive.resource_state_service import (
    audit_article,
    locate_target,
    needed,
    set_attempt,
)
from .completion_planner import (
    plan_article,
    requested_resources,
    selected_match,
    selection_rows,
)
from .main_flow_models import (
    MainFlowCommand,
    MainFlowContext,
    SingleArticleOptions,
    SingleArticleReceipt,
)


class MainFlowCoordinator:
    """把主页扫描和单篇任务按安全顺序串联起来。"""

    def __init__(
        self,
        *,
        scanner: Any,
        navigation_factory=None,
        dispatcher: Any,
        sleep: Callable[[float], None] = time.sleep,
        foreground_timeout_seconds: float = 120.0,
        post_task_timeout_seconds: float = 300.0,
        trace: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._navigation_factory = navigation_factory
        self._scanner = scanner
        self._dispatcher = dispatcher
        self._sleep = sleep
        self._foreground_timeout_seconds = max(0.1, float(foreground_timeout_seconds))
        self._post_task_timeout_seconds = max(0.1, float(post_task_timeout_seconds))
        self._trace = trace

    def run(self, context: MainFlowContext | Any, command: MainFlowCommand) -> None:
        state = context.state
        handles: list[Any] = []
        handled_fingerprints: set[str] = set()
        navigation = None
        session: Any | None = None
        flow_error: Exception | None = None
        article_error_reported = False
        selections = []
        selected_done = set()
        try:
            if str(state.snapshot().get("status")) in {"idle", "starting"}:
                state.start()
            selections = selection_rows(context, command)
            if selections:
                accounts = {r["account_name"] for r in selections}
                if len(accounts) != 1:
                    raise ValueError("每次补采仅支持一个公众号")
                state.set_account_name(next(iter(accounts)))
                for item in selections:
                    if context.cancel_token.is_set():
                        break
                    if item.get("article_id"):
                        key, resource_state = audit_article(
                            context.db_path, context.storage_root, item["article_id"]
                        )
                        missing = (
                            needed(resource_state, requested_resources(command))
                            if command.skip_collected_records
                            else list(requested_resources(command))
                        )
                        missing = self._dependencies(missing, resource_state)
                        if not missing:
                            state.handle_receipt(
                                SingleArticleReceipt(
                                    task_id=key,
                                    target_fingerprint=key,
                                    status="skipped_collected",
                                    foreground_done=True,
                                    tab_closed=True,
                                    message="本次所选资源均完整，跳过",
                                )
                            )
                            selected_done.add(item["target_key"])
                            continue
                        if not any(
                            r in missing for r in ["detail", "comments", "offline"]
                        ):
                            self._run_local(
                                context, item["article_id"], missing, key, key, command
                            )
                            selected_done.add(item["target_key"])
                if (
                    len(selected_done) == len(selections)
                    or context.cancel_token.is_set()
                ):
                    return
            if context.cancel_token.is_set():
                return
            if self._navigation_factory is not None:
                navigation = self._navigation_factory(context)
                name = selections[0]["account_name"] if selections else command.target_account_name.strip()
                mode = "automatic" if selections else command.account_mode
                self._set_action(state, "导航到公众号主页" if mode == "automatic" else "核验已打开的公众号主页")
                window = navigation.open(name, mode)
                if navigation.empty_home:
                    state.set_account_name(name)
                    state.set_task_info("公众号明确无文章，本轮零篇")
                    return
                session = self._scanner.initialize(command, window=window)
            else:
                session = self._scanner.initialize(command)
            if selections and session.account_name != selections[0]["account_name"]:
                raise ValueError("请先打开公众号主页：" + selections[0]["account_name"])
            account_name = str(getattr(session, "account_name", "") or "").strip()
            if account_name:
                state.set_account_name(account_name)

            while not context.cancel_token.is_set():
                if _target_limit_reached(handled_fingerprints, command.target_count):
                    break
                self._set_action(state, "正在识别文章卡片")
                batch = self._scanner.scan_visible_targets(session)
                dispatched = False
                for target in tuple(getattr(batch, "targets", ()) or ()):
                    if context.cancel_token.is_set():
                        break
                    if target.fingerprint in handled_fingerprints:
                        continue
                    if _target_limit_reached(handled_fingerprints, command.target_count):
                        break
                    selected = (
                        selected_match(target, selections) if selections else None
                    )
                    if selections and (
                        selected is None or selected["target_key"] in selected_done
                    ):
                        self._scanner.mark_processed(session, target)
                        continue
                    handled_fingerprints.add(target.fingerprint)
                    state.set_task_info(target.title_raw or target.title_display)
                    key, row, missing = plan_article(context, target, command)
                    if selected:
                        key = selected["target_key"]
                        selected_done.add(key)
                    if row:
                        _, resource_state = audit_article(
                            context.db_path, context.storage_root, row["id"]
                        )
                        missing = self._dependencies(missing, resource_state)
                    if not missing:
                        state.handle_receipt(
                            SingleArticleReceipt(
                                task_id=key,
                                target_fingerprint=target.fingerprint,
                                status="skipped_collected",
                                foreground_done=True,
                                tab_closed=True,
                                message="本次所选资源均完整，跳过",
                            )
                        )
                        self._scanner.mark_processed(session, target)
                        continue
                    if row and not any(
                        r in missing for r in ["detail", "comments", "offline"]
                    ):
                        self._run_local(
                            context,
                            row["id"],
                            missing,
                            key,
                            target.fingerprint,
                            command,
                        )
                        self._scanner.mark_processed(session, target)
                        continue
                    set_attempt(context.db_path, key, missing, "running")
                    options = SingleArticleOptions(
                        skip_collected_records=False,
                        target_key=key,
                        requested_resources=tuple(missing),
                        expected_article_link=row["article_link"] if row else "",
                        build_reader=command.build_reader
                        and (
                            "reader" in missing
                            or "body_images" in missing
                            or "comments" in missing
                        ),
                        collect_article_detail=True,
                        collect_comments="comments" in missing,
                        archive_offline="offline" in missing,
                        offline_archive_mode=command.offline_archive_mode,
                    )
                    self._set_action(state, "正在分发单篇任务")
                    handle = self._dispatcher.submit(target, options, context)
                    handles.append(handle)
                    dispatched = True
                    self._set_action(state, "正在等待文章标签关闭")
                    try:
                        foreground = self._wait_foreground_done(
                            handle,
                            context=context,
                        )
                    except Exception as exc:
                        state.handle_receipt(
                            SingleArticleReceipt(
                                task_id=str(getattr(handle, "handle_id", "")),
                                target_fingerprint=target.fingerprint,
                                status="failed",
                                phase="foreground",
                                error_stage="foreground",
                                error_detail=str(exc),
                                message="前台阶段未返回安全回执",
                            )
                        )
                        article_error_reported = True
                        self._trace_event(
                            "foreground-timeout", target=target, error=str(exc)
                        )
                        raise

                    state.handle_receipt(foreground)
                    self._drain_updates(handle, state)
                    self._scanner.mark_processed(session, target)
                    if not foreground.foreground_done:
                        raise RuntimeError("单篇任务未确认主页可以继续")
                    if command.single_task_interval_seconds > 0:
                        context.cancel_token.wait(command.single_task_interval_seconds)

                if (
                    (selections and len(selected_done) == len(selections))
                    or context.cancel_token.is_set()
                    or _target_limit_reached(handled_fingerprints, command.target_count)
                ):
                    break
                if bool(getattr(batch, "boundary_reached", False)):
                    break
                self._set_action(state, "正在滚动主页")
                if not self._scanner.scroll_next(session):
                    break
                if not dispatched:
                    # 空屏仍然允许扫描器执行下一次滚动；不在协调器中读取 UIA。
                    continue

        except Exception as exc:
            flow_error = exc
            self._trace_event("main-flow-failed", error=str(exc))
        finally:
            self._set_action(state, "正在清理运行资源")
            should_cancel = flow_error is not None or context.cancel_token.is_set()
            if should_cancel:
                if navigation is not None and navigation.recovered:
                    navigation.pacer.cancelled = lambda: True
                self._cancel_handles(handles)
            error_count_before = int(state.snapshot().get("errorCount") or 0)
            try:
                self._wait_post_tasks(handles, state, context)
            except Exception as exc:
                flow_error = flow_error or exc
            finally:
                if navigation is not None:
                    self._set_action(state, "清理本轮微信页面")
                    try:
                        safe = self._dispatcher.wait_foreground_idle(timeout_seconds=15)
                        cleanup = navigation.finish(close=command.close_home_after_task, foreground_safe=safe)
                    except Exception as exc:
                        cleanup = {"status": "failed", "errors": [str(exc)]}
                    state.set_cleanup_result(cleanup)
                    self._trace_event("cleanup-result", **cleanup)
            for item in selections:
                if item["target_key"] not in selected_done:
                    message = (
                        "补采已停止，可重新排队"
                        if context.cancel_token.is_set()
                        else (
                            "主页未定位到目标，请回到顶部后重试"
                            if flow_error is None
                            else str(flow_error)
                        )
                    )
                    locate_target(
                        context.db_path, item["target_key"], "not_found", message
                    )
                    set_attempt(
                        context.db_path,
                        item["target_key"],
                        requested_resources(command),
                        "failed",
                        message,
                    )
            if context.cancel_token.is_set():
                state.cancel()
            elif flow_error is not None:
                error_count_after = int(state.snapshot().get("errorCount") or 0)
                state.fail(
                    f"主流程异常：{type(flow_error).__name__}: {flow_error}",
                    count_error=(
                        not article_error_reported
                        and error_count_after == error_count_before
                    ),
                )
            elif state.snapshot().get("status") not in {"failed", "cancelled"}:
                state.complete()

    @staticmethod
    def _dependencies(missing, resource_state):
        result = list(missing)
        if (
            any(s in result for s in ["body_images", "reader"])
            and resource_state["detail"]["artifact_status"] != "complete"
        ):
            result.append("detail")
        if (
            "avatars" in result
            and resource_state["comments"]["artifact_status"] == "unknown"
        ):
            result.append("comments")
        return list(dict.fromkeys(result))

    def _run_local(self, context, article_id, resources, key, fingerprint, command):
        state = context.state
        state.set_task_info("补齐本地图片并重建阅读页")
        try:
            result = ReaderService(
                database_path=context.db_path,
                storage_root=context.storage_root,
                temp_root=context.temp_dir,
                project_root=Path(__file__).resolve().parents[3],
                write_coordinator=getattr(context, "write_coordinator", None),
            ).repair(
                article_id,
                resources,
                refresh=not command.skip_collected_records,
                comments_requested=command.collect_comments
                or "comments" in requested_resources(command),
                cancel=context.cancel_token,
            )
            ok = result["status"] == "complete"
            state.handle_receipt(
                SingleArticleReceipt(
                    task_id=key,
                    target_fingerprint=fingerprint,
                    status="success" if ok else "failed",
                    article_saved=True,
                    foreground_done=True,
                    tab_closed=True,
                    progress_counted=True,
                    message="本地资源已补齐"
                    if ok
                    else "仍有图片或评论缺失，请查看阶段状态",
                )
            )
        except Exception as exc:
            state.handle_receipt(
                SingleArticleReceipt(
                    task_id=key,
                    target_fingerprint=fingerprint,
                    status="failed",
                    progress_counted=True,
                    foreground_done=True,
                    tab_closed=True,
                    message=str(exc),
                )
            )

    def _wait_post_tasks(self, handles: list[Any], state: Any, context: Any) -> None:
        for handle in handles:
            try:
                final = self._dispatcher.wait_finished(
                    handle,
                    timeout_seconds=self._post_task_timeout_seconds,
                )
                state.handle_receipt(final)
                if final.status in {"failed", "cancelled"} and getattr(
                    handle.options, "target_key", ""
                ):
                    from src.services.archive.resource_state_service import states

                    key = handle.options.target_key
                    resource_states = states(context.db_path, key)
                    for resource in handle.options.requested_resources:
                        if resource_states[resource]["attempt_status"] in {
                            "pending",
                            "running",
                        }:
                            set_attempt(
                                context.db_path,
                                key,
                                [resource],
                                "failed",
                                final.error_detail or final.message or "采集未完成",
                            )
                    locate_target(
                        context.db_path,
                        key,
                        "failed",
                        final.error_detail or final.message,
                    )
                self._drain_updates(handle, state)
            except Exception as exc:
                target = getattr(handle, "target", None)
                fingerprint = str(getattr(target, "fingerprint", "") or "")
                if fingerprint:
                    state.handle_receipt(
                        SingleArticleReceipt(
                            task_id=str(getattr(handle, "handle_id", "")),
                            target_fingerprint=fingerprint,
                            status="failed",
                            phase="finished",
                            error_stage="post_task",
                            error_detail=str(exc),
                            message="后置任务等待失败",
                        )
                    )
                if context.cancel_token.is_set():
                    self._dispatcher.cancel(handle)

    def _cancel_handles(self, handles: list[Any]) -> None:
        cancel = getattr(self._dispatcher, "cancel", None)
        if not callable(cancel):
            return
        for handle in handles:
            try:
                cancel(handle)
            except Exception:
                continue

    def _wait_foreground_done(
        self,
        handle: Any,
        *,
        context: MainFlowContext | Any,
    ) -> Any:
        """以短轮询等待前台回执，使用户停止可以及时打断等待。"""

        deadline = time.monotonic() + self._foreground_timeout_seconds
        while True:
            if context.cancel_token.is_set():
                self._cancel_handles([handle])
                raise RuntimeError("主流程已请求停止")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("单篇任务未在规定时间返回前台回执")
            try:
                return self._dispatcher.wait_foreground_done(
                    handle,
                    timeout_seconds=min(0.2, remaining),
                )
            except TimeoutError:
                if context.cancel_token.is_set():
                    self._cancel_handles([handle])
                    raise RuntimeError("主流程已请求停止")
                if time.monotonic() >= deadline:
                    raise

    def _drain_updates(self, handle: Any, state: Any) -> None:
        poll = getattr(self._dispatcher, "poll_updates", None)
        updates = (
            poll(handle)
            if callable(poll)
            else list(getattr(handle, "updates", ()) or ())
        )
        for update in updates:
            receipt = getattr(update, "to_receipt", None)
            if callable(receipt):
                state.handle_receipt(receipt())

    def _set_action(self, state: Any, action: str) -> None:
        state.set_action(action)
        self._trace_event("action", action=action)

    def _trace_event(self, event: str, **details: Any) -> None:
        if self._trace is None:
            return
        try:
            self._trace({"event": event, "details": details})
        except Exception:
            return


def _target_limit_reached(selected_fingerprints: set[str], target_count: int) -> bool:
    if int(target_count) <= 0:
        return False
    # Limit the distinct articles in this run, not successful downloads.
    # Existing, failed and asynchronously finishing articles still occupy a slot.
    return len(selected_fingerprints) >= int(target_count)


__all__ = ["MainFlowCoordinator"]
