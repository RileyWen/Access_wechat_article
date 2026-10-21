from __future__ import annotations

from pathlib import Path
import shutil
from uuid import uuid4

from src.modules.archive.resource_manifest_builder import ResourceManifestBuilder
from src.services.archive.reader_resources import (
    VERSION,
    ImageDownloader,
    article_body,
    avatar_urls,
    body_image_urls,
    external_proxy,
    load_json,
    render_reader,
    write_json,
)
from src.services.archive.resource_commit_service import ResourceCommitService
from src.services.archive.resource_state_service import (
    audit_article,
    file_digest,
    needed,
    safe_archive,
    set_attempt,
)
from src.storage.repositories.article_repository import ArticleRepository
from src.storage.sqlite.connection import sqlite_connection


class ReaderService:
    """Repair only local-resource stages; WeChat credential acquisition is upstream."""

    def __init__(
        self,
        *,
        database_path,
        storage_root,
        temp_root,
        project_root=None,
        write_coordinator=None,
        downloader=None,
    ):
        self.db = Path(database_path)
        self.storage = Path(storage_root)
        self.temp = Path(temp_root)
        self.project = Path(project_root) if project_root else None
        self.coordinator = write_coordinator
        self.downloader = downloader

    def repair(
        self,
        article_id: int,
        requested=("avatars", "body_images", "reader"),
        *,
        refresh=False,
        comments_requested=True,
        cancel=None,
    ) -> dict:
        key, before = audit_article(self.db, self.storage, article_id)
        local = [s for s in requested if s in {"avatars", "body_images", "reader"}]
        if not refresh:
            local = needed(before, local)
        if not local:
            return {"articleId": article_id, "resources": before, "status": "complete"}
        with sqlite_connection(self.db, write=False) as conn:
            record = ArticleRepository(conn).get_by_id(article_id)
        root = safe_archive(self.storage, record.archive_dir)
        stage = self.temp / ("reader-" + uuid4().hex)
        backup = self.temp / (stage.name + "-backup")
        stage.mkdir(parents=True)
        set_attempt(self.db, key, local, "running")
        downloader = self.downloader or ImageDownloader(
            proxy=external_proxy(self.project), cancel=cancel
        )
        replacements = []
        attempt_overrides = {}
        try:
            package = load_json(root / "comments/final.json")
            avatars = load_json(root / "comments/assets/manifest.json")
            if "avatars" in local or (
                "reader" in local
                and package
                and before["avatars"]["artifact_status"] != "complete"
            ):
                if not package and before["comments"]["artifact_status"] != "complete":
                    raise ValueError("缺少评论数据，需先通过微信补采评论")
                avatars = downloader.download(
                    avatar_urls(package),
                    stage,
                    prefix="comments/assets/avatars",
                    old_root=root,
                    old_manifest=avatars,
                    refresh=refresh
                    or (
                        before["avatars"]["artifact_status"] == "complete"
                        and before["avatars"]["attempt_status"] in {"failed", "partial"}
                    ),
                )
                if (
                    avatars["status"] != "complete"
                    and before["avatars"]["artifact_status"] == "complete"
                ):
                    attempt_overrides["avatars"] = "本次头像下载不完整，继续保留旧版"
                    avatars = load_json(root / "comments/assets/manifest.json")
                else:
                    write_json(stage / "comments/assets/manifest.json", avatars)
                    replacements.append("comments/assets")
            reader_manifest = load_json(root / "reader/manifest.json")
            rebuild = "reader" in local or (root / "reader/index.html").is_file()
            if "body_images" in local or rebuild:
                source = (root / "origin/main.html").read_text(encoding="utf-8")
                detail = load_json(root / "article_detail.json")
                reader_manifest = downloader.download(
                    body_image_urls(article_body(source)),
                    stage,
                    prefix="reader/assets/img",
                    old_root=root,
                    old_manifest=reader_manifest,
                    refresh=(refresh and "body_images" in local)
                    or (
                        before["body_images"]["artifact_status"] == "complete"
                        and before["body_images"]["attempt_status"]
                        in {"failed", "partial"}
                    ),
                )
                if (
                    reader_manifest["status"] != "complete"
                    and before["body_images"]["artifact_status"] == "complete"
                ):
                    attempt_overrides["body_images"] = (
                        "本次正文图片下载不完整，继续保留旧版"
                    )
                    shutil.copytree(
                        root / "reader", stage / "reader", dirs_exist_ok=True
                    )
                    reader_manifest = load_json(root / "reader/manifest.json")
                reader_manifest.update(
                    version=VERSION,
                    source_sha256=file_digest(root / "origin/main.html"),
                    comments_sha256=file_digest(root / "comments/final.json"),
                    comments_requested=comments_requested,
                )
                write_json(stage / "reader/manifest.json", reader_manifest)
                # An empty article image inventory still needs a committed directory.
                (stage / "reader/assets/img").mkdir(parents=True, exist_ok=True)
                if rebuild:
                    (stage / "reader/index.html").write_text(
                        render_reader(
                            source,
                            detail,
                            package,
                            reader_manifest,
                            avatars,
                            comments_requested=comments_requested,
                        ),
                        encoding="utf-8",
                    )
                elif (root / "reader/index.html").exists():
                    shutil.copy2(
                        root / "reader/index.html", stage / "reader/index.html"
                    )
                replacements.append("reader")
            if cancel and cancel.is_set():
                raise RuntimeError("任务已停止，保留原有数据")

            def update(conn):
                manifest = ResourceManifestBuilder().build(root)
                from datetime import datetime

                ArticleRepository(conn).update_resource_manifest(
                    article_id,
                    manifest,
                    collected_time=datetime.now().isoformat(timespec="seconds"),
                )

            ResourceCommitService(write_coordinator=self.coordinator).commit(
                database_path=self.db,
                stage_root=stage,
                target_root=root,
                backup_root=backup,
                resource_paths=replacements,
                database_operation=update,
            )
            _, after = audit_article(self.db, self.storage, article_id)
            for resource in local:
                set_attempt(
                    self.db,
                    key,
                    [resource],
                    "partial"
                    if resource in attempt_overrides
                    else after[resource]["artifact_status"],
                    attempt_overrides.get(resource, after[resource]["reason"]),
                )
            _, after = audit_article(self.db, self.storage, article_id)
            return {
                "articleId": article_id,
                "resources": after,
                "status": "complete" if not needed(after, local) else "partial",
            }
        except Exception as exc:
            set_attempt(
                self.db,
                key,
                local,
                "failed",
                str(exc)
                if isinstance(exc, (ValueError, RuntimeError))
                else type(exc).__name__,
            )
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
