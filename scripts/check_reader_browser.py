"""Verify a generated reader with HTTP/HTTPS blocked, optionally capture comments."""

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        str(Path(__file__).resolve().parents[1] / ".playwright-browsers"),
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("article_dir", type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    page_path = args.article_dir.resolve() / "reader/index.html"
    if not page_path.is_file():
        parser.error("reader/index.html does not exist")
    requests = []
    with sync_playwright() as playwright:
        kwargs = {"headless": True}
        if args.executable:
            kwargs["executable_path"] = str(args.executable)
        browser = playwright.chromium.launch(**kwargs)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on(
            "request",
            lambda request: (
                requests.append(request.url)
                if request.url.startswith(("http://", "https://"))
                else None
            ),
        )
        page.route("http://**", lambda route: route.abort())
        page.route("https://**", lambda route: route.abort())
        page.goto(page_path.as_uri())
        page.locator("img").evaluate_all(
            "images => images.forEach(image => { image.loading = 'eager' })"
        )
        page.wait_for_function(
            "Array.from(document.images).every(image => image.complete && image.naturalWidth > 0)",
            timeout=15000,
        )
        comments = page.locator("section.comments")
        comments.scroll_into_view_if_needed()
        if args.screenshot:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.screenshot))
        result = {
            "imageCount": page.locator("img").count(),
            "messageCount": page.locator(".message").count(),
            "remoteRequests": len(requests),
            "offline": not requests,
        }
        browser.close()
    print(json.dumps(result))
    return 0 if result["offline"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
