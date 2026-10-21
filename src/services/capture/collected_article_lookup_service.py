from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.storage.repositories.account_repository import AccountRepository
from src.storage.repositories.article_repository import ArticleRecord, ArticleRepository
from src.storage.sqlite.connection import sqlite_connection


@dataclass(frozen=True, slots=True)
class CollectedArticleLookupResult:
    """已采集记录校验结果，用于前端展示具体命中或未命中原因。"""

    matched: bool
    reason: str
    account_name: str
    account_id: int | None = None
    record: ArticleRecord | None = None


class CollectedArticleLookupService:
    """通过公众号 ID 和完整标题检查文章是否已进入本地索引。"""

    def is_collected(
        self,
        *,
        database_path: str | Path,
        account_name: str,
        article_title: str,
        storage_root: str | Path | None = None,
        required_resources: tuple[str, ...] = ('detail',),
    ) -> bool:
        with sqlite_connection(database_path, write=False) as connection:
            account = AccountRepository(connection).get_by_name(account_name)
            if account is None:
                return False
            rows=connection.execute('SELECT id FROM awa_public_articles WHERE account_id=? AND article_title=?',(account.id,article_title)).fetchall()
        if len(rows)!=1:
            return False
        from src.services.archive.resource_state_service import audit_article,needed
        storage=Path(storage_root) if storage_root else Path(database_path).resolve().parents[2]/'storages'
        _,state=audit_article(Path(database_path),storage,int(rows[0]['id']))
        return not needed(state,required_resources)

    def find_by_account_date_and_title_fragment(
        self,
        *,
        database_path: str | Path,
        account_name: str,
        published_date: str,
        title_fragment: str,
    ) -> ArticleRecord | None:
        """按公众号名称、发布日期和标题片段查询本地文章索引。"""
        return self.lookup_by_account_date_and_title_fragment(
            database_path=database_path,
            account_name=account_name,
            published_date=published_date,
            title_fragment=title_fragment,
        ).record

    def lookup_by_account_date_and_title_fragment(
        self,
        *,
        database_path: str | Path,
        account_name: str,
        published_date: str,
        title_fragment: str,
    ) -> CollectedArticleLookupResult:
        """返回已采集记录校验详情，区分公众号不存在和文章未命中。"""
        normalized_account = account_name.strip()
        normalized_title = title_fragment.strip()
        if not normalized_account or not normalized_title:
            return CollectedArticleLookupResult(
                matched=False,
                reason="missing-input",
                account_name=normalized_account,
            )
        with sqlite_connection(database_path, write=False) as connection:
            account = AccountRepository(connection).get_by_name(normalized_account)
            if account is None:
                return CollectedArticleLookupResult(
                    matched=False,
                    reason="account-not-found",
                    account_name=normalized_account,
                )
            record = ArticleRepository(connection).find_by_account_date_and_title_fragment(
                account.id,
                published_date=published_date,
                title_fragment=normalized_title,
            )
            return CollectedArticleLookupResult(
                matched=record is not None,
                reason="matched" if record is not None else "article-not-found",
                account_name=normalized_account,
                account_id=account.id,
                record=record,
            )


__all__ = ["CollectedArticleLookupResult", "CollectedArticleLookupService"]
