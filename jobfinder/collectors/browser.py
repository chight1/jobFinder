"""Sites whose pages only load in a real browser (CareerCross, TokyoDev). Chromium through Playwright.

Same config as the plain-page collector (list_urls, job_link, page_param, whole_list, ...);
only the loading differs. Requests still take their turn through the fetcher, so the
politeness rules and the request log apply.

    headed   true opens a visible browser window. Some Cloudflare checks stop every headless
             browser but let a visible one through (TokyoDev); the window closes after the source.

Any other source can send just its job pages here with detail_browser (see runner.detail_loader),
when its list or feed loads plainly but its job pages are behind a check (startup.jobs).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as BrowserPage
from playwright.sync_api import Response as BrowserResponse
from playwright.sync_api import sync_playwright

from jobfinder.collectors import Context, Listing, PostingGone, Summary
from jobfinder.collectors.pages import list_from_pages, parse_job_page
from jobfinder.fetch import Fetcher, FetchError, SourceBlocked

LOAD_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 10_000
CHALLENGE_WAIT_S = 30

_local = threading.local()


def _page(config: dict[str, Any]) -> BrowserPage:
    """One browser per worker thread, opened on first use and closed by finish()."""
    if getattr(_local, "page", None) is None:
        _local.playwright = sync_playwright().start()
        if config.get("headed"):
            _local.browser = _local.playwright.chromium.launch(channel="chromium", headless=False)
        else:
            _local.browser = _local.playwright.chromium.launch(headless=True)
        _local.page = _local.browser.new_page(locale="en-US")
    return _local.page


def finish() -> None:
    if getattr(_local, "page", None) is not None:
        _local.browser.close()
        _local.playwright.stop()
        _local.page = None


def _load(ctx: Context, url: str) -> tuple[int, str, str]:
    page = _page(ctx.config)
    loads: list[BrowserResponse] = []  # every page load in the tab, a challenge's own reload included

    def on_response(response: BrowserResponse) -> None:
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            loads.append(response)

    page.on("response", on_response)
    try:
        with ctx.fetcher.slot(ctx.source_id, url) as record:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
                except PlaywrightError:
                    pass  # pages with long-polling never go idle; what has rendered is enough
                html = _wait_out_challenge(page, page.content())
            except PlaywrightError as exc:
                raise FetchError(f"browser could not load {url}: {exc}") from exc
            status = loads[-1].status if loads else 0  # the page it ended on, not the challenge before it
            record["status"], record["bytes"] = status, len(html)
    finally:
        page.remove_listener("response", on_response)
    if status == 403 or Fetcher.looks_like_challenge(html):
        raise SourceBlocked(f"challenge page at {url}")
    return status, html, page.url


def _wait_out_challenge(page: BrowserPage, html: str) -> str:
    """A visible browser usually passes a Cloudflare check by itself in a few seconds, and the check then
    reloads the page. Wait for that, up to CHALLENGE_WAIT_S; a check still showing after it is a block."""
    deadline = time.monotonic() + CHALLENGE_WAIT_S
    while Fetcher.looks_like_challenge(html) and time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        try:
            page.wait_for_load_state("domcontentloaded")
            html = page.content()
        except PlaywrightError:
            pass  # caught in the middle of the reload; look again
    return html


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    return list_from_pages(ctx, ctx.config, lambda url: _load(ctx, url))


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    status, html, _ = _load(ctx, summary.url)
    if status in (404, 410):
        raise PostingGone(summary.url)
    return parse_job_page(html, summary, ctx.config.get("selectors") or {})
