"""The only door to the network.

Every request goes through Fetcher, so the politeness rules hold everywhere:
one request at a time per site, a gap between requests, retries with backoff,
a raw-page cache so no page is fetched twice in one night, and a log row per request.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from jobfinder import db
from jobfinder.config import Config

Kind = Literal["html", "api"]

# A challenge page is small and says so. Large pages are skipped because ordinary job
# pages often load a captcha script for their apply form.
_CHALLENGE_MARKERS = ("just a moment", "cf-chl-", "attention required", "verify you are human", "captcha")
_CHALLENGE_MAX_CHARS = 50_000
_MAX_RETRY_AFTER_S = 300.0  # a site asking for a longer wait is treated as a block
_RETRYABLE = (429, 502, 503, 504)
_PUSHBACK = (429, 503)  # the site asking for fewer requests: still refused after the retries, that is a block
_RETURNED = (404, 410)  # callers need these: a dead board, a closed posting


class FetchError(Exception):
    """A request failed in a way that affects only this page. status is the HTTP status, when there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NetworkError(FetchError):
    """No answer at all: the connection failed or dropped, or the request timed out."""


class SourceBlocked(FetchError):
    """The site pushed back: stop this source for the night."""


class DailyCapReached(FetchError):
    """The source's stated daily request limit is used up."""


@dataclass(frozen=True)
class Page:
    url: str  # final URL after redirects
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class _Site:
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_at: float = 0.0  # time.monotonic() before which no request may start


class Fetcher:
    def __init__(self, cfg: Config, *, run_started: datetime, dry_run: bool = False) -> None:
        self._cfg = cfg.fetch
        self._raw_dir = cfg.raw_dir
        self._db_path = cfg.db_path
        self._run_started = run_started.timestamp()
        self._dry_run = dry_run
        self._sites: dict[str, _Site] = {}
        self._sites_lock = threading.Lock()
        self._local = threading.local()
        self._clients: list[httpx.Client] = []
        self._clients_lock = threading.Lock()
        self._gaps: dict[str, float | None] = {}
        self._caps: dict[str, int | None] = {}
        conn = db.connect(self._db_path)
        try:
            for row in conn.execute("SELECT id, base_delay_s, min_delay_s, daily_cap FROM sources"):
                self._gaps[row["id"]] = row["min_delay_s"] or row["base_delay_s"]
                self._caps[row["id"]] = row["daily_cap"]
        finally:
            conn.close()

    # --- public ---------------------------------------------------------------

    def get(
        self,
        source_id: str,
        url: str,
        *,
        kind: Kind = "html",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
        retries: int | None = None,
    ) -> Page:
        """timeout_s and retries replace config.toml's for this request: a slow server, or a caller that
        does its own waiting."""
        return self._request(source_id, "GET", url, kind=kind, params=params, headers=headers, timeout_s=timeout_s,
                             retries=retries)

    def post_json(self, source_id: str, url: str, body: Any, *, kind: Kind = "api") -> Page:
        return self._request(source_id, "POST", url, kind=kind, json_body=body)

    def download(self, source_id: str, url: str, dest: Path) -> None:
        """Save a binary file (a zip, a tarball) to disk, with the same pacing and logging."""
        site = self._site(url)
        with site.lock:
            self._wait(site)
            started = time.monotonic()
            nbytes, status, error = 0, None, None
            try:
                with self._client().stream("GET", url) as resp:
                    status = resp.status_code
                    if resp.is_success:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with open(dest, "wb") as f:
                            for chunk in resp.iter_bytes():
                                f.write(chunk)
                                nbytes += len(chunk)
            except httpx.TransportError as exc:
                error = repr(exc)
            finally:
                self._log(source_id, url, status, nbytes, started, error)
                site.next_at = time.monotonic() + self._gap_s(source_id, "api")
        if error or status is None or not 200 <= status < 300:
            raise FetchError(f"download of {url} failed: {error or status}")

    @contextmanager
    def slot(self, source_id: str, url: str, *, kind: Kind = "html") -> Iterator[dict[str, Any]]:
        """Take the site's turn for a request made elsewhere (the headless browser).

        The caller fills record["status"] and record["bytes"]; the request is logged
        and the site's gap applied the same way as for get().
        """
        self._check_cap(source_id)
        site = self._site(url)
        record: dict[str, Any] = {"status": None, "bytes": 0, "error": None}
        with site.lock:
            self._wait(site)
            started = time.monotonic()
            try:
                yield record
            except Exception as exc:
                record["error"] = repr(exc)
                raise
            finally:
                self._log(source_id, url, record["status"], record["bytes"], started, record["error"])
                site.next_at = time.monotonic() + self._gap_s(source_id, kind)

    @staticmethod
    def looks_like_challenge(text: str) -> bool:
        if len(text) > _CHALLENGE_MAX_CHARS:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in _CHALLENGE_MARKERS)

    def close(self) -> None:
        with self._clients_lock:
            for client in self._clients:
                client.close()
            self._clients.clear()

    # --- request path ---------------------------------------------------------

    def _request(
        self,
        source_id: str,
        method: str,
        url: str,
        *,
        kind: Kind,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout_s: float | None = None,
        retries: int | None = None,
    ) -> Page:
        full_url = str(httpx.URL(url, params=params)) if params else url
        cache_path = self._cache_path(source_id, method, full_url, json_body)
        cached = self._cache_read(cache_path, full_url)
        if cached:
            return cached

        self._check_cap(source_id)
        client = self._client()
        site = self._site(full_url)
        retries = int(self._cfg["retries"]) if retries is None else retries
        with site.lock:
            for attempt in range(retries + 1):
                self._wait(site)
                started = time.monotonic()
                try:
                    resp = client.request(method, full_url, headers=headers, json=json_body,
                                          timeout=httpx.USE_CLIENT_DEFAULT if timeout_s is None else timeout_s)
                except httpx.TransportError as exc:
                    self._log(source_id, full_url, None, 0, started, repr(exc))
                    site.next_at = time.monotonic() + self._gap_s(source_id, kind)
                    if attempt < retries:
                        time.sleep(float(self._cfg["retry_wait_s"]))
                        continue
                    raise NetworkError(f"network error after {retries} retries: {exc!r}") from exc

                self._log(source_id, full_url, resp.status_code, len(resp.content), started, None)
                site.next_at = time.monotonic() + self._gap_s(source_id, kind)
                status = resp.status_code

                if status in _RETRYABLE:
                    wait = self._retry_after_s(resp)
                    if wait > _MAX_RETRY_AFTER_S:
                        raise SourceBlocked(f"HTTP {status}, Retry-After {wait:.0f}s", status)
                    if attempt < retries:
                        time.sleep(wait)
                        continue
                    if status in _PUSHBACK:
                        raise SourceBlocked(f"HTTP {status} after {retries} retries", status)
                    raise FetchError(f"HTTP {status} after {retries} retries for {full_url}", status)  # a gateway kept failing
                if status == 403:
                    raise SourceBlocked("HTTP 403", status)
                if "html" in resp.headers.get("content-type", "") and self.looks_like_challenge(resp.text):
                    raise SourceBlocked("challenge page")
                if resp.is_success or status == 304 or status in _RETURNED:
                    page = Page(str(resp.url), status, resp.text, dict(resp.headers))
                    if status == 200:
                        self._cache_write(cache_path, page.text)
                    return page
                raise FetchError(f"HTTP {status} for {full_url}")
        raise AssertionError("unreachable")

    def _retry_after_s(self, resp: httpx.Response) -> float:
        default = float(self._cfg["retry_wait_s"])
        value = resp.headers.get("retry-after")
        if not value:
            return default
        if value.strip().isdigit():
            return float(value)
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return default
        return max(0.0, (when - datetime.now(UTC)).total_seconds())

    # --- politeness -------------------------------------------------------------

    def _site(self, url: str) -> _Site:
        host = urlsplit(url).hostname or url
        with self._sites_lock:
            return self._sites.setdefault(host, _Site())

    @staticmethod
    def _wait(site: _Site) -> None:
        delay = site.next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _gap_s(self, source_id: str, kind: Kind) -> float:
        base = self._gaps.get(source_id) or float(self._cfg[f"{kind}_gap_s"])
        return base + random.uniform(0, float(self._cfg[f"{kind}_jitter_s"]))

    def _check_cap(self, source_id: str) -> None:
        cap = self._caps.get(source_id)
        if not cap:
            return
        since = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
        conn = self._conn()
        used = conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE source_id = ? AND at >= ? AND status IS NOT NULL",
            (source_id, since),
        ).fetchone()[0]
        if used >= cap:
            raise DailyCapReached(f"{used} of {cap} daily requests used")

    # --- per-thread resources -----------------------------------------------------

    def _client(self) -> httpx.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = httpx.Client(
                follow_redirects=True,
                timeout=float(self._cfg["timeout_s"]),
                headers={"User-Agent": self._cfg["user_agent"], "Accept-Language": "en"},
            )
            self._local.client = client
            with self._clients_lock:
                self._clients.append(client)
        return client

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self._db_path)
            self._local.conn = conn
        return conn

    def _log(
        self, source_id: str, url: str, status: int | None, nbytes: int, started: float, error: str | None
    ) -> None:
        if self._dry_run:
            return
        ms = int((time.monotonic() - started) * 1000)
        self._conn().execute(
            "INSERT INTO fetch_log (source_id, url, status, bytes, ms, at, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, url, status, nbytes, ms, db.utc_now(), error),
        )

    # --- raw cache --------------------------------------------------------------

    def _cache_path(self, source_id: str, method: str, url: str, body: Any) -> Path:
        key = hashlib.sha256(f"{method} {url} {json.dumps(body, sort_keys=True)}".encode()).hexdigest()
        return self._raw_dir / source_id / f"{key}.html.gz"

    def _cache_read(self, path: Path, url: str) -> Page | None:
        """A page saved since this run started answers instead of the network."""
        try:
            if path.stat().st_mtime < self._run_started:
                return None
            return Page(url, 200, gzip.decompress(path.read_bytes()).decode("utf-8"), from_cache=True)
        except FileNotFoundError:
            return None

    def _cache_write(self, path: Path, text: str) -> None:
        if self._dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(text.encode("utf-8")))
