"""Sites whose pages only load in a real browser (CareerCross). Headless Chromium through Playwright.

Same config as the plain-page collector (list_urls, job_link, page_param, ...); only
the loading differs. Requests still take their turn through the fetcher, so the
politeness rules and the request log apply.
"""

from __future__ import annotations

import re
import threading

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as BrowserPage
from playwright.sync_api import sync_playwright

from jobhunt.collectors import Context, Listing, PostingGone, Summary
from jobhunt.collectors.pages import job_links, page_url, parse_job_page
from jobhunt.fetch import Fetcher, FetchError, SourceBlocked

LOAD_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 10_000

_local = threading.local()


def _page() -> BrowserPage:
    """One headless browser per worker thread, opened on first use and closed by finish()."""
    if getattr(_local, "page", None) is None:
        _local.playwright = sync_playwright().start()
        _local.browser = _local.playwright.chromium.launch(headless=True)
        _local.page = _local.browser.new_page(locale="en-US")
    return _local.page


def finish() -> None:
    if getattr(_local, "page", None) is not None:
        _local.browser.close()
        _local.playwright.stop()
        _local.page = None


def _load(ctx: Context, url: str) -> tuple[int, str]:
    page = _page()
    with ctx.fetcher.slot(ctx.source_id, url) as record:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            except PlaywrightError:
                pass  # pages with long-polling never go idle; what has rendered is enough
            html = page.content()
        except PlaywrightError as exc:
            raise FetchError(f"browser could not load {url}: {exc}") from exc
        status = response.status if response else 0
        record["status"], record["bytes"] = status, len(html)
    if status == 403 or Fetcher.looks_like_challenge(html):
        raise SourceBlocked(f"challenge page at {url}")
    return status, html


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    config = ctx.config
    pattern = re.compile(config["job_link"])
    paged = config.get("page_param") or config.get("page_url")
    max_pages = int(config.get("max_pages", 10)) if paged else 1
    found: dict[str, Summary] = {}
    for list_url in config["list_urls"]:
        for number in range(1, max_pages + 1):
            if ctx.out_of_time():
                break
            url = page_url(list_url, number, config)
            status, html = _load(ctx, url)
            if status >= 400:
                break
            fresh = {i: s for i, s in job_links(html, url, pattern) if i not in found}
            if not fresh:
                break
            found |= fresh
            if not (set(fresh) - ctx.known(fresh)):
                break
    return Listing(list(found.values()), set(found))


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    status, html = _load(ctx, summary.url)
    if status in (404, 410):
        raise PostingGone(summary.url)
    return parse_job_page(html, summary, ctx.config.get("selectors") or {})
