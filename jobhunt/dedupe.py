"""Merging sightings into jobs, so the model never reads the same position twice.

Level 1 is the canonical link, level 2 an exact company + title + city key,
level 3 a fuzzy comparison against recently seen jobs at the same company.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable, Iterable
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from datasketch import MinHash, MinHashLSH
from pydantic import BaseModel

from jobhunt import db
from jobhunt.llm import LLM, posting_for_prompt
from jobhunt.rules import Gazetteer
from jobhunt.text import nfkc, words

# --- level 1: same link ---------------------------------------------------------

_TRACKING_PARAMS = frozenset({"ref", "source", "gh_src", "lever-source", "lever-origin"})


def canonical_url(url: str) -> str:
    """https, lowercase host, no fragment, no trailing slash, no tracking parameters."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit(("https", host, parts.path.rstrip("/"), urlencode(query), ""))


# --- level 2: same key ------------------------------------------------------------

LEGAL_SUFFIXES = frozenset({"inc", "llc", "ltd", "co", "corp", "gmbh", "kk"})
_JAPANESE_LEGAL = ("株式会社", "合同会社", "(株)")
_BRACKETED = re.compile(r"[(\[{（【「][^)\]}）】」]*[)\]}）】」]")
_PUNCTUATION = re.compile(r"[^\w\s]")


def company_norm(raw: str, aliases: dict[str, str] | None = None) -> str:
    text = nfkc(raw or "").lower()  # NFKC also turns ㈱ into (株)
    for suffix in _JAPANESE_LEGAL:
        text = text.replace(suffix, " ")
    text = _PUNCTUATION.sub(" ", text.replace(".", ""))  # "K.K." -> "kk", "Co.,Ltd." -> "co ltd"
    norm = " ".join(t for t in text.split() if t not in LEGAL_SUFFIXES)
    return (aliases or {}).get(norm, norm)


def title_norm(raw: str, gaz: Gazetteer) -> str:
    text = _BRACKETED.sub(" ", nfkc(raw or "").lower())
    text = gaz.remove_places(text)
    text = re.sub(r"\bsr\b", "senior", text)
    text = re.sub(r"\bjr\b", "junior", text)
    return " ".join(text.split())


def city_norm(location: str, gaz: Gazetteer) -> str:
    place = gaz.first_city(location or "")
    return place.name.lower() if place else "remote"


def is_agency(norm: str, agencies: Iterable[str]) -> bool:
    """A post that hides the employer: no company at all, or a known recruiting agency."""
    if not norm:
        return True
    padded = f" {norm} "
    return any(f" {company_norm(agency)} " in padded for agency in agencies)


# --- level 3: fuzzy, same company only --------------------------------------------------

MERGE_TITLE_SIM = 0.6
MERGE_DESC_SIM = 0.8
BORDERLINE_DESC_SIM = 0.5
SHINGLE_WORDS = 5

FuzzyVerdict = Literal["merge", "borderline", "different"]


def shingles(text: str, size: int = SHINGLE_WORDS) -> set[str]:
    """Every run of `size` consecutive words."""
    tokens = words(text)
    if len(tokens) < size:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def fuzzy_verdict(title_a: str, text_a: str, title_b: str, text_b: str) -> tuple[FuzzyVerdict, float, float]:
    title_sim = jaccard(set(words(title_a)), set(words(title_b)))
    desc_sim = jaccard(shingles(text_a), shingles(text_b))
    if title_sim >= MERGE_TITLE_SIM and desc_sim >= MERGE_DESC_SIM:
        verdict: FuzzyVerdict = "merge"
    elif BORDERLINE_DESC_SIM <= desc_sim < MERGE_DESC_SIM:
        verdict = "borderline"
    else:
        verdict = "different"
    return verdict, title_sim, desc_sim


# --- agency posts: MinHash over every open job ---------------------------------------------

_NUM_PERM = 128


def _minhash(text: str) -> MinHash:
    mh = MinHash(num_perm=_NUM_PERM)
    mh.update_batch([s.encode("utf-8") for s in shingles(text)])
    return mh


class AgencyIndex:
    """Finds jobs whose description is at least 0.8 similar to an agency post.

    Built on first use from every open job's description, then kept current as jobs
    are added during the run. Shared by the fetch threads, so access is locked.
    """

    def __init__(self, load: Callable[[], Iterable[tuple[int, str]]]) -> None:
        self._load = load
        self._lock = threading.Lock()
        self._lsh: MinHashLSH | None = None
        self._hashes: dict[int, MinHash] = {}

    def _ensure_built(self) -> MinHashLSH:
        if self._lsh is None:
            self._lsh = MinHashLSH(threshold=MERGE_DESC_SIM, num_perm=_NUM_PERM)
            for job_id, text in self._load():
                self._insert(job_id, text)
        return self._lsh

    def _insert(self, job_id: int, text: str) -> None:
        if job_id in self._hashes or not text:
            return
        mh = _minhash(text)
        self._hashes[job_id] = mh
        assert self._lsh is not None
        self._lsh.insert(str(job_id), mh)

    def add(self, job_id: int, text: str) -> None:
        with self._lock:
            if self._lsh is not None:
                self._insert(job_id, text)

    def best_match(self, text: str) -> tuple[int, float] | None:
        """The most similar open job at 0.8 or above, with its estimated similarity."""
        if not text:
            return None
        mh = _minhash(text)
        with self._lock:
            lsh = self._ensure_built()
            candidates = [(int(key), self._hashes[int(key)].jaccard(mh)) for key in lsh.query(mh)]
        candidates = [c for c in candidates if c[1] >= MERGE_DESC_SIM]
        return max(candidates, key=lambda c: c[1]) if candidates else None


# --- borderline pairs: the model decides --------------------------------------------------


class SameOrDifferent(BaseModel):
    answer: Literal["same", "different"]


def judge_pair(conn: sqlite3.Connection, llm: LLM, job_a: int, job_b: int) -> str:
    """Ask the model whether two postings at one company describe the same position."""
    prompt = (
        "Two job postings from the same company follow. Do they describe the same position "
        "(the same role, possibly reposted or listed in two places), or different positions?\n"
        'Answer with JSON: {"answer": "same"} or {"answer": "different"}.\n\n'
        f"=== Posting 1 ===\n{posting_for_prompt(conn, job_a, cap=3000)}\n\n"
        f"=== Posting 2 ===\n{posting_for_prompt(conn, job_b, cap=3000)}"
    )
    return llm.chat_json("dupe", job_b, [{"role": "user", "content": prompt}], SameOrDifferent).answer


def merge_jobs(conn: sqlite3.Connection, keep: int, drop: int) -> None:
    """Fold one job into another: its sightings move over and the dropped row goes."""
    with db.transaction(conn):
        conn.execute("UPDATE sightings SET job_id = ? WHERE job_id = ?", (keep, drop))
        dropped = conn.execute("SELECT status, status_at, follow_up_on FROM jobs WHERE id = ?", (drop,)).fetchone()
        if dropped and dropped["status"] != "none":  # keep anything the candidate already did with it
            conn.execute(
                "UPDATE jobs SET status = ?, status_at = ?, follow_up_on = ? WHERE id = ? AND status = 'none'",
                (dropped["status"], dropped["status_at"], dropped["follow_up_on"], keep),
            )
        conn.execute("DELETE FROM packets WHERE job_id = ?", (drop,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (drop,))  # dupe_pairs rows go with it
