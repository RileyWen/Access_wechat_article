"""Validate the saved page rather than treating every browser warning as loss."""

from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree, html

from src.services.archive.reader_resources import article_body, image_ok


def offline_cache_state(root: Path, manifest: dict) -> tuple[str, int, int, str]:
    root = root.resolve()
    index = root / "index.html"
    if not index.is_file():
        return "unknown", 0, 1, "尚未生成网页缓存"
    resources = {}
    images = set()
    invalid = set()

    def reference(value, *, image=False):
        value = str(value or "").strip()
        if not value or value.startswith(("data:", "#")):
            return
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            resources[value] = None
            return
        path = (root / unquote(parsed.path)).resolve()
        if not path.is_relative_to(root):
            resources[value] = None
            return
        key = path.relative_to(root).as_posix()
        resources[key] = path
        if image:
            images.add(key)

    for value in manifest.get("files", []):
        reference(value)
    try:
        source = index.read_text(encoding="utf-8")
        article_body(source)
        tree = html.fromstring(source)
        for node in tree.xpath("//img | //video | //audio | //source | //link"):
            if node.tag == "link":
                if "stylesheet" in (node.get("rel") or "").split():
                    reference(node.get("href"))
                continue
            reference(node.get("src") or node.get("data-src"), image=node.tag == "img")
            reference(node.get("poster"), image=True)
    except (OSError, ValueError, TypeError, etree.LxmlError):
        invalid.add("正文结构")
    for key, path in resources.items():
        if path is None or not path.is_file() or not path.stat().st_size:
            invalid.add(key)
        elif key in images:
            if path.suffix.lower() == ".svg":
                try:
                    parser = etree.XMLParser(resolve_entities=False, no_network=True)
                    if not etree.fromstring(path.read_bytes(), parser).tag.endswith(
                        "svg"
                    ):
                        invalid.add(key)
                except etree.XMLSyntaxError:
                    invalid.add(key)
            elif not image_ok(path):
                invalid.add(key)
    warnings = manifest.get("warnings", [])
    loading_incomplete = any("达到最长滚动加载时间" in str(w) for w in warnings)
    complete = not invalid and not loading_incomplete
    completed = sum(key not in invalid for key in resources)
    if complete:
        reason = (
            f"另有 {len(warnings)} 条非关键下载提示，页面所需本地资源已核验"
            if warnings
            else ""
        )
    else:
        reasons = []
        if invalid:
            reasons.append(f"缺少或无效的必要资源 {len(invalid)} 项")
        if loading_incomplete:
            reasons.append("页面滚动加载未结束")
        reason = "；".join(reasons)
    expected = len(resources) + int("正文结构" in invalid)
    return "complete" if complete else "partial", completed, expected, reason
