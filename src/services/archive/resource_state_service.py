"""Persistent, evidence-based archive completeness, separate from last attempt."""

from __future__ import annotations

from datetime import datetime
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
from threading import RLock
import unicodedata

from src.services.archive.reader_resources import (
    VERSION,
    article_body,
    avatar_urls,
    body_image_urls,
    comments_completeness,
    image_ok,
    load_json,
)
from src.storage.sqlite.connection import sqlite_connection


STAGES = ("detail", "comments", "avatars", "body_images", "reader", "offline")
LABELS = dict(
    zip(
        STAGES,
        ("文章详情", "评论及回复", "评论头像", "正文图片", "重建阅读页", "网页缓存"),
    )
)
STATUSES = {
    "not_requested",
    "pending",
    "running",
    "complete",
    "partial",
    "failed",
    "unknown",
}
_schema_lock = RLock()


def normalized_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).replace("……", "...").replace("…", "...")
    return " ".join(text.split())


def ensure_schema(database_path: Path) -> None:
    with _schema_lock:
        with sqlite_connection(database_path) as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='awa_resource_state'"
            ).fetchone():
                return
            # SQLite's backup API includes committed WAL pages, unlike copying the file.
            backup = Path(str(database_path) + ".before-reader-v1.bak")
            if not backup.exists():
                with closing(sqlite3.connect(backup)) as destination:
                    conn.backup(destination)
            conn.executescript("""
CREATE TABLE IF NOT EXISTS awa_repair_targets (
 target_key TEXT PRIMARY KEY, article_id INTEGER UNIQUE,
 account_name TEXT NOT NULL, title TEXT NOT NULL, published_date TEXT NOT NULL,
 locate_status TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL,
 FOREIGN KEY(article_id) REFERENCES awa_public_articles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS awa_resource_state (
 target_key TEXT NOT NULL, stage TEXT NOT NULL,
 artifact_status TEXT NOT NULL DEFAULT 'unknown', completed INTEGER NOT NULL DEFAULT 0,
 expected INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
 attempt_status TEXT NOT NULL DEFAULT 'not_requested', attempt_time TEXT NOT NULL DEFAULT '',
 attempt_error TEXT NOT NULL DEFAULT '', requested INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(target_key,stage),
 FOREIGN KEY(target_key) REFERENCES awa_repair_targets(target_key) ON DELETE CASCADE
);
""")


def recover_interrupted(database_path: Path) -> None:
    ensure_schema(database_path)
    with sqlite_connection(database_path) as conn:
        conn.execute(
            "UPDATE awa_resource_state SET attempt_status='pending', attempt_error='上次运行中断，可重新排队' WHERE attempt_status='running'"
        )


def register_target(
    db: Path, account: str, title: str, date: str, article_id=None
) -> str:
    ensure_schema(db)
    key = hashlib.sha256(
        (account + "\n" + date[:10] + "\n" + normalized_title(title)).encode()
    ).hexdigest()[:32]
    with sqlite_connection(db) as conn:
        if article_id:
            existing = conn.execute(
                "SELECT target_key FROM awa_repair_targets WHERE article_id=?",
                (article_id,),
            ).fetchone()
            if existing:
                return existing[0]
        conn.execute(
            """INSERT INTO awa_repair_targets(target_key,article_id,account_name,title,published_date,updated_at)
 VALUES(?,?,?,?,?,?) ON CONFLICT(target_key) DO UPDATE SET article_id=COALESCE(excluded.article_id,awa_repair_targets.article_id),updated_at=excluded.updated_at""",
            (
                key,
                article_id,
                account,
                title,
                date[:10],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO awa_resource_state(target_key,stage) VALUES(?,?)",
            [(key, s) for s in STAGES],
        )
    return key


def locate_target(db: Path, key: str, status: str, message: str = "", article_id=None):
    with sqlite_connection(db) as conn:
        if article_id:
            other = conn.execute(
                "SELECT target_key FROM awa_repair_targets WHERE article_id=?",
                (article_id,),
            ).fetchone()
            if other and other[0] != key:
                conn.execute(
                    """INSERT INTO awa_resource_state
 (target_key,stage,artifact_status,completed,expected,reason,attempt_status,attempt_time,attempt_error,requested)
 SELECT ?,stage,artifact_status,completed,expected,reason,attempt_status,attempt_time,attempt_error,requested
 FROM awa_resource_state WHERE target_key=?
 ON CONFLICT(target_key,stage) DO UPDATE SET
 artifact_status=excluded.artifact_status,completed=excluded.completed,expected=excluded.expected,reason=excluded.reason,
 attempt_status=CASE WHEN excluded.attempt_time>=awa_resource_state.attempt_time THEN excluded.attempt_status ELSE awa_resource_state.attempt_status END,
 attempt_time=MAX(excluded.attempt_time,awa_resource_state.attempt_time),
 attempt_error=CASE WHEN excluded.attempt_time>=awa_resource_state.attempt_time THEN excluded.attempt_error ELSE awa_resource_state.attempt_error END,
 requested=MAX(excluded.requested,awa_resource_state.requested)""",
                    (key, other[0]),
                )
                conn.execute(
                    "DELETE FROM awa_repair_targets WHERE target_key=?", (other[0],)
                )
        conn.execute(
            "UPDATE awa_repair_targets SET locate_status=?,message=?,article_id=COALESCE(?,article_id),updated_at=? WHERE target_key=?",
            (
                status,
                message,
                article_id,
                datetime.now().isoformat(timespec="seconds"),
                key,
            ),
        )


def set_attempt(db: Path, key: str, stages, status: str, error: str = ""):
    if status not in STATUSES:
        raise ValueError(status)
    with sqlite_connection(db) as conn:
        for stage in stages:
            if stage not in STAGES:
                raise ValueError(stage)
            conn.execute(
                """UPDATE awa_resource_state SET requested=1,attempt_status=?,attempt_time=?,attempt_error=? WHERE target_key=? AND stage=?""",
                (
                    status,
                    datetime.now().isoformat(timespec="seconds"),
                    error,
                    key,
                    stage,
                ),
            )


def states(db: Path, key: str) -> dict:
    with sqlite_connection(db, write=False) as conn:
        rows = conn.execute(
            "SELECT * FROM awa_resource_state WHERE target_key=?", (key,)
        ).fetchall()
    return {r["stage"]: dict(r) | {"label": LABELS[r["stage"]]} for r in rows}


def needed(state: dict, requested) -> list[str]:
    return [
        s
        for s in requested
        if state.get(s, {}).get("artifact_status") != "complete"
        or state.get(s, {}).get("attempt_status")
        in {"failed", "partial", "running", "pending"}
    ]


def safe_archive(storage: Path, archive_dir: str) -> Path:
    result = (storage / archive_dir).resolve()
    if not result.is_relative_to(storage.resolve()):
        raise ValueError("归档目录超出存储根目录")
    return result


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def manifest_state(root: Path, manifest: dict, urls: list[str]) -> tuple:
    files = manifest.get("files", {})
    completed = 0
    for url in urls:
        item = files.get(url, {})
        path = (root / item.get("path", "")).resolve()
        if (
            item.get("status") == "complete"
            and path.is_relative_to(root.resolve())
            and image_ok(path, item.get("sha256", ""))
        ):
            completed += 1
    return (
        "complete" if completed == len(urls) else "partial",
        completed,
        len(urls),
        "" if completed == len(urls) else "部分本地图片缺失、损坏或未下载",
    )


def audit_article(db: Path, storage: Path, article_id: int) -> tuple[str, dict]:
    ensure_schema(db)
    with sqlite_connection(db, write=False) as conn:
        row = conn.execute(
            """SELECT ar.*,a.account_name FROM awa_public_articles ar JOIN awa_public_accounts a ON a.id=ar.account_id WHERE ar.id=?""",
            (article_id,),
        ).fetchone()
    if row is None:
        raise LookupError("文章不存在")
    key = register_target(
        db,
        row["account_name"],
        row["article_title"],
        row["published_article_time"],
        article_id,
    )
    root = safe_archive(storage, row["archive_dir"])
    detail = load_json(root / "article_detail.json")
    source_path = root / "origin/main.html"
    source = source_path.read_text(encoding="utf-8") if source_path.is_file() else ""
    checks = {}
    try:
        body = article_body(source)
        image_urls = body_image_urls(body)
        valid = bool(
            source
            and normalized_title(str(detail.get("article_title") or ""))
            == normalized_title(row["article_title"])
            and (detail.get("article_short_link") or detail.get("article_link"))
            == row["article_link"]
            and detail.get("account_name") == row["account_name"]
        )
        checks["detail"] = (
            "complete" if valid else "partial",
            int(valid),
            1,
            "" if valid else "详情字段不全",
        )
    except Exception:
        image_urls = None
        checks["detail"] = ("partial", 0, 1, "缺少有效原始正文或详情")
    package = load_json(root / "comments/final.json")
    if package:
        checks["comments"] = comments_completeness(package)
        checks["avatars"] = manifest_state(
            root,
            load_json(root / "comments/assets/manifest.json"),
            avatar_urls(package),
        )
    else:
        from src.modules.request.article_parser import extract_html_comment_count

        count = extract_html_comment_count(source) if source else None
        checks["comments"] = (
            ("complete", 0, 0, "已明确无评论")
            if count == 0
            else ("unknown", 0, 0, "尚无有效评论数据")
        )
        checks["avatars"] = (
            ("complete", 0, 0, "") if count == 0 else ("unknown", 0, 0, "需先获取评论")
        )
    reader_manifest = load_json(root / "reader/manifest.json")
    checks["body_images"] = (
        manifest_state(root, reader_manifest, image_urls)
        if image_urls is not None
        else ("unknown", 0, 0, "需先获取有效正文")
    )
    reader = root / "reader/index.html"
    valid_reader = (
        reader.is_file()
        and reader_manifest.get("version") == VERSION
        and reader_manifest.get("source_sha256") == file_digest(source_path)
        and reader_manifest.get("comments_sha256", "")
        == file_digest(root / "comments/final.json")
    )
    reader_reason = "阅读页未生成或源数据已更新"
    if valid_reader:
        tree = dom_tree(reader)
        for src in tree.xpath("//@src"):
            local = (reader.parent / src).resolve()
            if (
                ":" in src
                or not local.is_relative_to(root.resolve())
                or not local.is_file()
            ):
                valid_reader = False
                reader_reason = "阅读页引用缺失或含远程资源"
                break
    dependencies = [checks["body_images"][0]]
    if reader_manifest.get("comments_requested", True) or package:
        dependencies += [checks["comments"][0], checks["avatars"][0]]
    reader_complete = valid_reader and all(s == "complete" for s in dependencies)
    checks["reader"] = (
        "complete" if reader_complete else "partial",
        int(valid_reader),
        1,
        ""
        if reader_complete
        else ("关联资源不完整" if valid_reader else reader_reason),
    )
    from src.services.archive.offline_completeness import offline_cache_state

    checks["offline"] = offline_cache_state(
        root, load_json(root / "assets/awa-manifest.json")
    )
    with sqlite_connection(db) as conn:
        for stage, (status, completed, expected, reason) in checks.items():
            conn.execute(
                "UPDATE awa_resource_state SET artifact_status=?,completed=?,expected=?,reason=? WHERE target_key=? AND stage=?",
                (status, completed, expected, reason, key, stage),
            )
        if checks["offline"][0] == "complete":
            # Re-evaluate only the earlier blanket-warning verdict. An actual
            # failed refresh must remain visible even when its saved page works.
            conn.execute(
                """UPDATE awa_resource_state SET attempt_status='complete',attempt_error=''
 WHERE target_key=? AND stage='offline' AND attempt_status='partial'
 AND attempt_error='原离线资源存在缺失或下载警告'""",
                (key,),
            )
    return key, states(db, key)


def dom_tree(path: Path):
    from lxml import html

    return html.fromstring(path.read_text(encoding="utf-8"))


def pending_targets(db: Path, account_name: str = "") -> list[dict]:
    ensure_schema(db)
    with sqlite_connection(db, write=False) as conn:
        rows = conn.execute(
            "SELECT * FROM awa_repair_targets WHERE article_id IS NULL"
            + (" AND account_name=?" if account_name else "")
            + " ORDER BY updated_at DESC",
            (account_name,) if account_name else (),
        ).fetchall()
    return [dict(r) | {"resources": states(db, r["target_key"])} for r in rows]
