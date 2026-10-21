from __future__ import annotations

from pathlib import Path
from typing import Iterable

from src.domain.enums import ResourceType
from src.domain.models import ResourceManifest


RESOURCE_LOCATIONS: tuple[tuple[ResourceType, tuple[Path, ...]], ...] = (
    (ResourceType.ARTICLE_DETAIL, (Path("article_detail.json"),)),
    (ResourceType.ORIGIN_HTML, (Path("origin/main.html"),)),
    (ResourceType.ORIGIN_REQUEST, (Path("origin/request.json"),)),
    (ResourceType.COMMENT_DETAIL, (Path("comments/final.json"), Path("comments_final.json"))),
    (ResourceType.COMMENT_ASSETS, (Path("comments/assets"), Path("comment_assets"))),
    (ResourceType.READER_HTML, (Path("reader/index.html"),)),
    (ResourceType.READER_ASSETS, (Path("reader/assets"),)),
    (ResourceType.OFFLINE_HTML, (Path("index.html"),)),
    (ResourceType.OFFLINE_ASSETS, (Path("assets"),)),
)


class ResourceManifestBuilder:
    """以本地真实资源和本次即将替换的资源生成快速索引。"""

    def build(
        self,
        article_root: str | Path,
        *,
        planned_paths: Iterable[str | Path] = (),
    ) -> ResourceManifest:
        root = Path(article_root)
        planned = {Path(value).as_posix().casefold() for value in planned_paths}
        found: list[ResourceType] = []
        for resource_type, candidates in RESOURCE_LOCATIONS:
            if any(
                candidate.as_posix().casefold() in planned
                or _contains_resource(root / candidate)
                for candidate in candidates
            ):
                found.append(resource_type)
        return ResourceManifest.from_types(found)


def _contains_resource(path: Path) -> bool:
    if path.is_file():
        return True
    if not path.is_dir():
        return False
    try:
        return any(item.is_file() for item in path.rglob("*"))
    except OSError:
        return False
