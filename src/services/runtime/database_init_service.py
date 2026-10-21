from __future__ import annotations

from pathlib import Path

from src.config.app_config import AppConfig
from src.storage.sqlite.database_initializer import DatabaseInitializer
from src.storage.sqlite.schema_loader import load_schema


class DatabaseInitService:
    """只在程序启动阶段初始化或验证当前版本数据库。"""

    def __init__(self, *, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def initialize(self, config: AppConfig) -> Path:
        schema = load_schema(
            config.software.data_schema_version,
            project_root=self.project_root,
        )
        database_path = DatabaseInitializer().initialize(
            config.storage.database_path,
            schema.sql,
        )
        from src.services.archive.resource_state_service import recover_interrupted
        recover_interrupted(database_path)
        return database_path
