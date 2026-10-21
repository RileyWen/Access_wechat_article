"""Local-only article/comment rendering and verified image downloads."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
from html import escape
from io import BytesIO
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.request import getproxies

from lxml import html as dom
from PIL import Image
import requests


VERSION = 1
MAX_IMAGE_BYTES = 20 * 1024 * 1024
FORMATS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "AVIF": ".avif",
}


def load_json(path: Path, default=None):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return (
            value if isinstance(value, dict) else ({} if default is None else default)
        )
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def image_ok(path: Path, expected_hash: str = "") -> bool:
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            return False
        if (
            expected_hash
            and hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash
        ):
            return False
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
        return True
    except Exception:
        return False


def external_proxy(project_root: Path | None = None) -> str:
    explicit = os.environ.get("AWA_RESOURCE_PROXY", "")
    if not explicit and project_root:
        from src.config.config_loader import load_layered_config_mapping

        mapping = load_layered_config_mapping(project_root / "data/custom.yaml")
        explicit = str(mapping.get("resource_download", {}).get("proxy_url") or "")
        if not explicit:
            explicit = str(
                load_json(project_root / "data/runtime/resource-download.json").get(
                    "proxy_url"
                )
                or ""
            )
    if explicit:
        parsed = urlsplit(explicit)
        if (
            parsed.scheme not in {"http", "https", "socks5", "socks5h"}
            or not parsed.hostname
        ):
            raise ValueError("资源下载代理地址无效")
        if (
            parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.port == 18000
        ):
            raise ValueError("资源下载不能使用临时 MITM 代理")
        return explicit
    proxies = getproxies()
    candidate = proxies.get("https") or proxies.get("http") or ""
    parsed = urlsplit(candidate)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return ""
    return candidate


def normalize_url(value: Any, base: str = "https://mp.weixin.qq.com/") -> str:
    text = str(value or "").strip()
    if not text or text.startswith(("data:", "javascript:", "file:", "#")):
        return ""
    url = urljoin(base, text)
    return url if urlsplit(url).scheme in {"http", "https"} else ""


def messages(package: dict):
    """Only traverse actual message nodes, not arbitrary API metadata."""
    for comment in package.get("comments", []):
        if not isinstance(comment, dict):
            continue
        yield comment
        replies = (comment.get("reply_new") or {}).get("reply_list", [])
        for reply in replies:
            if isinstance(reply, dict):
                yield reply


def avatar_urls(package: dict) -> list[str]:
    return list(
        dict.fromkeys(
            url
            for message in messages(package)
            if (
                url := normalize_url(
                    message.get("logo_url") or message.get("avatar_url")
                )
            )
        )
    )


def comments_completeness(package: dict) -> tuple[str, int, int, str]:
    summary = package.get("summary", {})
    comments = package.get("comments")
    if not isinstance(comments, list):
        return "unknown", 0, 0, "缺少有效评论列表"
    replies = [message for message in messages(package)]
    actual = len(replies)
    expected = len(comments)
    for comment in comments:
        rn = comment.get("reply_new") or {}
        saved = len(rn.get("reply_list") or [])
        expected += max(
            saved,
            int(rn.get("reply_total_cnt", comment.get("reply_total_cnt", saved)) or 0),
        )
    reason = str(summary.get("stop_reason") or "")
    if actual < expected:
        return "partial", actual, expected, "回复数量不足"
    for target in (summary.get("reply_fetch") or {}).get("targets", []):
        if int(target.get("saved_reply_count", 0)) < int(
            target.get("expected_reply_count", 0)
        ):
            return "partial", actual, expected, "回复分页未完成"
    # Repeated pages are not proof that the server has no next page.
    if summary.get("pagination_complete") is True and reason in {
        "continue_flag_false",
        "confirmed_empty",
    }:
        return "complete", actual, expected, ""
    return "partial", actual, expected, reason or "缺少评论分页结束证据"


class ImageDownloader:
    def __init__(self, *, proxy: str = "", get=None, cancel=None):
        self.proxy = proxy
        self.get = get
        self.cancel = cancel

    def _fetch(self, url: str) -> bytes:
        if self.cancel and self.cancel.is_set():
            raise RuntimeError("任务已停止")
        kwargs = dict(
            timeout=(5, 10),
            stream=True,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://mp.weixin.qq.com/",
            },
            proxies={"http": self.proxy or None, "https": self.proxy or None},
        )
        session = requests.Session()
        session.trust_env = False
        try:
            with (self.get or session.get)(url, **kwargs) as response:
                response.raise_for_status()
                if (
                    not response.headers.get("Content-Type", "")
                    .split(";")[0]
                    .lower()
                    .startswith("image/")
                ):
                    raise ValueError("响应不是图片")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    if self.cancel and self.cancel.is_set():
                        raise RuntimeError("任务已停止")
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ValueError("图片超过 20 MiB")
                    chunks.append(chunk)
                return b"".join(chunks)
        finally:
            session.close()

    def download(
        self,
        urls: list[str],
        stage: Path,
        *,
        prefix: str,
        old_root: Path | None = None,
        old_manifest: dict | None = None,
        refresh: bool = False,
    ) -> dict:
        import shutil

        previous = (old_manifest or {}).get("files", {})
        unique = list(dict.fromkeys(urls))

        def one(url):
            if self.cancel and self.cancel.is_set():
                return url, {"status": "failed", "error": "任务已停止"}
            old = previous.get(url, {})
            if not refresh and old_root and old.get("status") == "complete":
                old_path = (old_root / old.get("path", "")).resolve()
                if old_path.is_relative_to(old_root.resolve()) and image_ok(
                    old_path, old.get("sha256", "")
                ):
                    target = stage / old["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(old_path, target)
                    return url, old
            error = "下载失败"
            for _ in range(2):
                try:
                    blob = self._fetch(url)
                    with Image.open(BytesIO(blob)) as image:
                        suffix = FORMATS.get(image.format)
                        image.verify()
                    with Image.open(BytesIO(blob)) as image:
                        image.load()
                    if not suffix:
                        raise ValueError("不支持的图片格式")
                    relative = (
                        prefix
                        + "/"
                        + hashlib.sha256(url.encode()).hexdigest()[:24]
                        + suffix
                    )
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blob)
                    return url, {
                        "path": relative,
                        "status": "complete",
                        "size": len(blob),
                        "sha256": hashlib.sha256(blob).hexdigest(),
                    }
                except Exception as exc:
                    # Do not expose request URLs, credentials, or proxy secrets in errors.
                    error = type(exc).__name__ + (
                        ": " + str(exc) if isinstance(exc, ValueError) else ""
                    )
                    if isinstance(exc, requests.HTTPError) and exc.response is not None:
                        error = f"HTTP {exc.response.status_code}"
                    if self.cancel and self.cancel.is_set():
                        break
            return url, {"status": "failed", "error": error}

        with ThreadPoolExecutor(max_workers=4) as executor:
            files = dict(executor.map(one, unique))
        complete = sum(item.get("status") == "complete" for item in files.values())
        return {
            "version": VERSION,
            "expected": len(unique),
            "completed": complete,
            "status": "complete" if complete == len(unique) else "partial",
            "files": files,
        }


def article_body(source: str):
    tree = dom.fromstring(source)
    nodes = tree.xpath('//*[@id="js_content"]')
    if not nodes:
        raise ValueError("原始 HTML 缺少文章正文 js_content，需重新获取文章")
    body = nodes[0]
    for node in list(body.iterdescendants()):
        if node.tag in {
            "script",
            "style",
            "iframe",
            "object",
            "embed",
            "form",
            "link",
            "meta",
            "video",
            "audio",
        }:
            node.drop_tree()
    return body


def body_image_urls(body) -> list[str]:
    urls = []
    for node in body.iter():
        if node.tag == "img":
            url = normalize_url(node.get("data-src") or node.get("src"))
            if url:
                urls.append(url)
        for raw in re.findall(r"url\(\s*['\"]?([^)'\"]+)", node.get("style", ""), re.I):
            if url := normalize_url(raw):
                urls.append(url)
    return list(dict.fromkeys(urls))


def local_ref(entry: dict, *, reader: bool = False) -> str:
    path = entry.get("path", "") if entry.get("status") == "complete" else ""
    return (
        "../" + path
        if path.startswith("comments/") and reader
        else path.removeprefix("reader/")
    )


def render_reader(
    source: str,
    detail: dict,
    package: dict,
    images: dict,
    avatars: dict,
    *,
    comments_requested: bool = True,
) -> str:
    body = article_body(source)
    allowed = {
        "div",
        "section",
        "p",
        "span",
        "br",
        "hr",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "pre",
        "code",
        "a",
        "img",
        "sub",
        "sup",
        "caption",
        "figure",
        "figcaption",
    }
    for node in list(body.iter()):
        if not isinstance(node.tag, str):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
            continue
        if node is not body and node.tag not in allowed:
            node.drop_tag()
            continue
        original_style = node.get("style", "")
        src = (
            normalize_url(node.get("data-src") or node.get("src"))
            if node.tag == "img"
            else ""
        )
        href = normalize_url(node.get("href")) if node.tag == "a" else ""
        retained = {
            k: v for k, v in node.attrib.items() if k in {"colspan", "rowspan", "alt"}
        }
        node.attrib.clear()
        node.attrib.update(retained)
        if node.tag == "img":
            local = local_ref(images.get("files", {}).get(src, {}))
            if local:
                node.set("src", local)
                node.set("loading", "lazy")
            else:
                node.tag = "span"
                node.text = "[图片未保存]"
                node.set("class", "missing")
        elif href:
            node.set("href", href)
            node.set("rel", "noreferrer")
        backgrounds = re.findall(r"url\(\s*['\"]?([^)'\"]+)", original_style, re.I)
        for raw in backgrounds:
            local = local_ref(images.get("files", {}).get(normalize_url(raw), {}))
            child = dom.Element("img") if local else dom.Element("span")
            if local:
                child.set("src", local)
            else:
                child.text = "[背景图片未保存]"
            node.insert(0, child)

    def message_html(message, reply=False):
        url = normalize_url(message.get("logo_url") or message.get("avatar_url"))
        avatar = local_ref(avatars.get("files", {}).get(url, {}), reader=True)
        portrait = (
            f'<img class="avatar" src="{escape(avatar, quote=True)}" alt="头像">'
            if avatar
            else '<span class="avatar fallback" aria-label="无本地头像">人</span>'
        )
        nickname = escape(
            str(message.get("nick_name") or message.get("nickname") or "匿名读者")
        )
        content = escape(str(message.get("content") or "")).replace("\n", "<br>")
        timestamp = message.get("create_time", "")
        try:
            timestamp = datetime.fromtimestamp(int(timestamp)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (ValueError, TypeError, OSError):
            timestamp = ""
        author = (
            ' <span class="badge">作者</span>'
            if (
                str(message.get("identity_type")) == "1"
                and str(message.get("is_from")) == "2"
            )
            else ""
        )
        likes = int(message.get("like_num") or message.get("reply_like_num") or 0)
        return f'<div class="message {"reply" if reply else "comment"}">{portrait}<div><b>{nickname}</b>{author}<small>{escape(str(timestamp))} · 赞 {likes}</small><p>{content}</p></div></div>'

    comment_rows = []
    for comment in package.get("comments", []):
        comment_rows.append(message_html(comment))
        for reply in (comment.get("reply_new") or {}).get("reply_list", []):
            comment_rows.append(message_html(reply, True))
    comment_state = comments_completeness(package)[0] if package else "unknown"
    warnings = []
    if not comments_requested and not package:
        warnings.append("本次未请求评论")
    elif comment_state != "complete":
        warnings.append("评论或回复尚未确认完整")
    for label, manifest in [("正文图片", images), ("头像", avatars)]:
        if manifest.get("completed", 0) < manifest.get("expected", 0):
            warnings.append(
                f"{label} {manifest.get('completed', 0)}/{manifest.get('expected', 0)}，存在未保存资源"
            )
    warning_html = "".join(f'<p class="notice">{escape(w)}</p>' for w in warnings)
    title = escape(str(detail.get("article_title") or "文章"))
    meta = (
        escape(str(detail.get("account_name") or ""))
        + " · "
        + escape(str(detail.get("published_article_time") or ""))
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file: data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{title}</title><style>
body{{margin:auto;max-width:820px;padding:32px 22px;background:#faf9f5;color:#252525;font:17px/1.9 system-ui,sans-serif}}
img{{max-width:100%;height:auto}}table{{border-collapse:collapse;max-width:100%;display:block;overflow:auto}}td,th{{border:1px solid #ccc;padding:8px}}
small,.meta{{display:block;color:#777;font-size:13px}}.comments{{border-top:1px solid #ccc;margin-top:40px}}.message{{display:flex;gap:12px;padding:18px 0;border-bottom:1px solid #e6e6e6}}.message>div{{min-width:0;flex:1}}.message p{{white-space:normal;overflow-wrap:anywhere;margin:4px 0}}.avatar{{width:40px;height:40px;object-fit:cover;border-radius:50%;flex:none}}.fallback{{display:inline-block;background:#ddd;text-align:center;color:#666}}.reply{{margin-left:50px;font-size:15px}}.notice,.missing{{color:#925b10;background:#fff0d4;padding:8px}}.badge{{font-size:12px;color:#21665a}}pre{{overflow:auto}}blockquote{{border-left:3px solid #ccc;padding-left:16px}}
</style></head><body><h1>{title}</h1><p class="meta">{meta}</p>{warning_html}<article>{dom.tostring(body, encoding="unicode")}</article>
<section class="comments"><h2>留言与回复</h2>{"".join(comment_rows) or "<p>暂无已保存的留言。</p>"}</section></body></html>"""
