"""Plain-text helpers shared by the rules, dedupe and model stages."""

from __future__ import annotations

import re
import unicodedata

from selectolax.lexbor import LexborHTMLParser

_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")
_BLOCKS = "p,div,section,article,header,footer,ul,ol,table,tr,blockquote,pre,dd,dt"
_HEADINGS = "h1,h2,h3,h4,h5,h6"
_SPACES = re.compile(r"[ \t 　]+")
_BLANK_RUN = re.compile(r"\n{3,}")


def nfkc(text: str) -> str:
    """NFKC turns full-width letters and digits into ordinary ones."""
    return unicodedata.normalize("NFKC", text)


def is_html(text: str) -> bool:
    return bool(_TAG.search(text))


def html_to_text(html: str) -> str:
    """HTML to plain text, one block per line, headings marked with '## '."""
    tree = LexborHTMLParser(html)
    for node in tree.css("script,style,noscript,template,svg"):
        node.decompose()
    for node in tree.css("br"):
        node.replace_with("\n")
    for node in tree.css(_HEADINGS):
        node.insert_before("\n## ")
        node.insert_after("\n")
    for node in tree.css("li"):
        node.insert_before("\n- ")
    for node in tree.css(_BLOCKS):
        node.insert_before("\n")
        node.insert_after("\n")
    root = tree.body or tree.root
    return tidy(root.text(deep=True, separator="") if root else "")


def tidy(text: str) -> str:
    lines = (_SPACES.sub(" ", line).strip() for line in text.splitlines())
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def to_text(description: str | None) -> str:
    if not description:
        return ""
    return html_to_text(description) if is_html(description) else tidy(description)


def words(text: str) -> list[str]:
    return re.findall(r"\w+", nfkc(text).lower())


MANY_PLACES = 10  # more places than this ("JP; AF; AX; ...") means the job is open almost anywhere


def short_location(location: str) -> str:
    """A location listing a long run of places, shortened to "Anywhere (N places)"."""
    places = [p for p in location.split(";") if p.strip()]
    return f"Anywhere ({len(places)} places)" if len(places) > MANY_PLACES else location
