"""Shared matching and resource planning for scans and selected retries."""

from src.services.archive.resource_state_service import (
    audit_article,
    needed,
    normalized_title,
    register_target,
)
from src.storage.sqlite.connection import sqlite_connection


def requested_resources(command):
    if command.retry_resources:
        return tuple(command.retry_resources)
    result = ["detail"]
    if command.collect_comments:
        result += ["comments", "avatars"]
    if command.build_reader:
        result += ["body_images", "reader"]
    if command.archive_offline:
        result += ["offline"]
    return tuple(result)


def find_article(db, account, title, date=""):
    with sqlite_connection(db, write=False) as conn:
        rows = conn.execute(
            """SELECT ar.* FROM awa_public_articles ar JOIN awa_public_accounts a ON a.id=ar.account_id
 WHERE a.account_name=? AND (?='' OR ar.published_article_time LIKE ?)""",
            (account, date[:10], date[:10] + "%"),
        ).fetchall()
    needle = normalized_title(title)
    exact = [r for r in rows if normalized_title(r["article_title"]) == needle]
    matches = exact or [
        r for r in rows if needle and needle in normalized_title(r["article_title"])
    ]
    if len(matches) > 1:
        raise ValueError("同一日期标题匹配不唯一，需人工确认")
    return dict(matches[0]) if matches else None


def plan_article(context, target, command):
    row = find_article(
        context.db_path,
        target.account_name,
        target.title_raw or target.title_display,
        target.article_date,
    )
    resources = requested_resources(command)
    if row:
        key, state = audit_article(context.db_path, context.storage_root, row["id"])
        missing = (
            needed(state, resources)
            if command.skip_collected_records
            else list(resources)
        )
    else:
        key = register_target(
            context.db_path,
            target.account_name,
            target.title_raw or target.title_display,
            target.article_date,
        )
        missing = list(resources)
    return key, row, missing


def selection_rows(context, command):
    if not command.retry_article_ids and not command.retry_target_keys:
        return []
    result = []
    with sqlite_connection(context.db_path, write=False) as conn:
        if command.retry_article_ids:
            placeholders = ",".join("?" for _ in command.retry_article_ids)
            rows = conn.execute(
                f"""SELECT ar.id AS article_id,ar.article_title AS title,ar.published_article_time AS published_date,a.account_name
 FROM awa_public_articles ar JOIN awa_public_accounts a ON a.id=ar.account_id WHERE ar.id IN ({placeholders})""",
                command.retry_article_ids,
            ).fetchall()
            for row in rows:
                item = dict(row)
                item["target_key"] = register_target(
                    context.db_path,
                    item["account_name"],
                    item["title"],
                    item["published_date"],
                    item["article_id"],
                )
                result.append(item)
        if command.retry_target_keys:
            placeholders = ",".join("?" for _ in command.retry_target_keys)
            result += [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM awa_repair_targets WHERE target_key IN ({placeholders})",
                    command.retry_target_keys,
                ).fetchall()
            ]
    return result


def selected_match(target, selections):
    title = normalized_title(target.title_raw or target.title_display)
    exact = [
        s
        for s in selections
        if s["account_name"] == target.account_name
        and (
            not s["published_date"]
            or s["published_date"][:10] == target.article_date[:10]
        )
        and normalized_title(s["title"]) == title
    ]
    matches = exact or [
        s
        for s in selections
        if s["account_name"] == target.account_name
        and (
            not s["published_date"]
            or s["published_date"][:10] == target.article_date[:10]
        )
        and title
        and title in normalized_title(s["title"])
    ]
    if len(matches) > 1:
        raise ValueError("选定补采目标存在标题歧义")
    return matches[0] if matches else None
