"""Removal rules and the queue score.

Code rules run on every new sighting before the model reads anything and remove
only clear cases. Fact rules run once the model has filled in a job's facts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from jobfinder import score
from jobfinder.config import Profile
from jobfinder.text import nfkc

# --- gazetteer ------------------------------------------------------------------

PlaceKind = Literal["city", "country", "region"]


@dataclass(frozen=True)
class Place:
    name: str
    country: str  # ISO 3166 alpha-2; "" for a multi-country region
    admin1: str  # first-level division name, e.g. "Tokyo", "Osaka"
    population: int
    kind: PlaceKind


# In ISO 3166-2 order, so prefecture N is the code JP-N.
JAPAN_PREFECTURES = {
    "Hokkaido": "北海道", "Aomori": "青森", "Iwate": "岩手", "Miyagi": "宮城", "Akita": "秋田",
    "Yamagata": "山形", "Fukushima": "福島", "Ibaraki": "茨城", "Tochigi": "栃木", "Gunma": "群馬",
    "Saitama": "埼玉", "Chiba": "千葉", "Tokyo": "東京", "Kanagawa": "神奈川", "Niigata": "新潟",
    "Toyama": "富山", "Ishikawa": "石川", "Fukui": "福井", "Yamanashi": "山梨", "Nagano": "長野",
    "Gifu": "岐阜", "Shizuoka": "静岡", "Aichi": "愛知", "Mie": "三重", "Shiga": "滋賀",
    "Kyoto": "京都", "Osaka": "大阪", "Hyogo": "兵庫", "Nara": "奈良", "Wakayama": "和歌山",
    "Tottori": "鳥取", "Shimane": "島根", "Okayama": "岡山", "Hiroshima": "広島", "Yamaguchi": "山口",
    "Tokushima": "徳島", "Kagawa": "香川", "Ehime": "愛媛", "Kochi": "高知", "Fukuoka": "福岡",
    "Saga": "佐賀", "Nagasaki": "長崎", "Kumamoto": "熊本", "Oita": "大分", "Miyazaki": "宮崎",
    "Kagoshima": "鹿児島", "Okinawa": "沖縄",
}
JAPAN_REGIONS = {
    "Japan": "日本", "Nippon": "", "Kanto": "関東", "Kansai": "関西", "Kinki": "近畿", "Chubu": "中部",
    "Tokai": "東海", "Hokuriku": "北陸", "Tohoku": "東北", "Chugoku": "中国地方", "Shikoku": "四国",
    "Kyushu": "九州",
}
KANSAI_PREFECTURES = frozenset({"osaka", "kyoto", "hyogo", "nara", "shiga", "wakayama"})

# Regions outside Japan. Asia and APAC are not here: they keep a job.
OUTSIDE_REGIONS = (
    "EMEA", "Europe", "EU", "European Union", "North America", "Latin America", "LATAM", "LatAm",
    "South America", "Central America", "Americas", "Africa", "Middle East", "MENA", "Oceania",
    "Nordics", "DACH", "Benelux", "Scandinavia", "Balkans", "CEE", "UK&I",
)
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
COUNTRY_ALIASES = {
    "usa": "US", "u.s": "US", "u.s.a": "US", "united states of america": "US", "america": "US",
    "uk": "GB", "u.k": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "britain": "GB",
    "great britain": "GB", "korea": "KR", "south korea": "KR", "holland": "NL",
    "the netherlands": "NL", "uae": "AE", "czechia": "CZ", "türkiye": "TR", "turkiye": "TR",
}

# Words that never name a place in a location string, even when a town shares the name.
_NOT_PLACES = frozenset(
    "remote hybrid onsite on site office home work from in the only based or and of time zone full "
    "part flexible any location locations multiple various city area region country friendly first "
    "team mobile us-based eu-based within with to for at".split()
)
_ANYWHERE = frozenset({"worldwide", "anywhere", "global"})
_ANYWHERE_IN = re.compile(r"\b(?:anywhere|worldwide|global)\s+(?:in|within)\b", re.IGNORECASE)  # "anywhere in India"
# Places after these are the ones a job is NOT open to. Not "outside": UK posts say "outside IR35".
_EXCLUSION = re.compile(r"\b(?:not\s+(?:in|from)|except|excluding)\b", re.IGNORECASE)
_ASIA = re.compile(r"\basia\b|\bapac\b|asia[- ]?pacific", re.IGNORECASE)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")
_TOKEN = re.compile(r"[^\W\d_]+(?:['.&-][^\W\d_]+)*")
_MAX_NGRAM = 4
_PART_BREAK = re.compile(r"[,;/|()\[\]·•]|\s[-–—]\s")
_EDGE_PUNCTUATION = ".,;:!?()[]{}|/-–—\"'“”‘’"  # not + or #: "C++", "C#"


def _cjk_rank(place: Place) -> tuple[bool, int]:
    """Which city a shared kanji name means: a Japanese one first, then the larger."""
    return place.country == "JP", place.population


class Gazetteer:
    """Matches place names in location text against GeoNames cities, countries and regions."""

    def __init__(
        self,
        places: Iterable[Place],
        cjk_names: dict[str, Place] | None = None,
        codes: dict[str, Place] | None = None,
    ) -> None:
        self._by_name: dict[str, Place] = {}
        self._codes = {code.upper(): place for code, place in (codes or {}).items()}  # ISO codes, US states
        self._cjk: dict[str, Place] = dict(cjk_names or {})
        for place in places:
            self._add(place.name, place)
        self._japan = self._codes.get("JP") or Place("Japan", "JP", "", 0, "country")
        self._add_japan_regions()
        self._add_outside_regions()

    # --- building -----------------------------------------------------------------

    def _add(self, name: str, place: Place) -> None:
        key = name.strip().lower()
        if not key:
            return
        current = self._by_name.get(key)
        # Countries and regions outrank cities; among cities the larger one wins.
        if (
            current is None
            or (current.kind == "city" and place.kind != "city")
            or (current.kind == place.kind == "city" and place.population > current.population)
        ):
            self._by_name[key] = place

    def _add_japan_regions(self) -> None:
        for name, kanji in JAPAN_PREFECTURES.items():
            prefecture = Place(name, "JP", name, 0, "region")
            existing = self._by_name.get(name.lower())
            if existing is None or existing.country != "JP":
                self._by_name[name.lower()] = prefecture
            if kanji:
                self._add_japanese_cjk(kanji, prefecture)
        for name, kanji in JAPAN_REGIONS.items():
            region = self._japan if name == "Japan" else Place(name, "JP", "", 0, "region")
            self._by_name[name.lower()] = region
            if kanji:
                self._add_japanese_cjk(kanji, region)
        self._codes["JP"] = self._japan

    def _add_japanese_cjk(self, name: str, place: Place) -> None:
        """A Japanese city of the same name stays; a city elsewhere gives way."""
        current = self._cjk.get(name)
        if current is None or current.country != "JP":
            self._cjk[name] = place

    def _add_outside_regions(self) -> None:
        for name in OUTSIDE_REGIONS:
            self._by_name.setdefault(name.lower(), Place(name, "", "", 0, "region"))
        for code, state in US_STATES.items():
            place = Place(state, "US", state, 0, "region")
            self._add(state, place)
            self._codes.setdefault(code, place)  # an ISO country code with the same letters wins
        for alias, code in COUNTRY_ALIASES.items():
            self._by_name.setdefault(alias, self._codes.get(code) or Place(alias, code, "", 0, "country"))

    @classmethod
    def from_geonames(cls, directory: Path) -> Gazetteer:
        """Load cities15000.txt, countryInfo.txt and admin1CodesASCII.txt from GeoNames."""
        admin1: dict[str, str] = {}
        with open(directory / "admin1CodesASCII.txt", encoding="utf-8") as f:
            for line in f:
                code, name, ascii_name, *_ = line.rstrip("\n").split("\t")
                admin1[code] = ascii_name or name

        places: list[Place] = []
        codes: dict[str, Place] = {}
        with open(directory / "countryInfo.txt", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                cols = line.rstrip("\n").split("\t")
                country = Place(cols[4], cols[0], "", int(cols[7] or 0), "country")
                places.append(country)
                codes[cols[0]] = codes[cols[1]] = country  # ISO alpha-2 and alpha-3

        cjk: dict[str, Place] = {}
        with open(directory / "cities15000.txt", encoding="utf-8") as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                country = cols[8]
                city = Place(cols[1], country, admin1.get(f"{country}.{cols[10]}", ""), int(cols[14] or 0), "city")
                places.append(city)
                if cols[2] and cols[2] != cols[1]:
                    places.append(Place(cols[2], city.country, city.admin1, city.population, "city"))
                # Kanji and hanzi names, so "上海" or "深圳" is placed as surely as "大阪". A name two
                # countries share goes to the Japanese city; a one-character name only to a Japanese one.
                for alt in cols[3].split(","):
                    if _CJK.search(alt) and (country == "JP" or len(alt) > 1):
                        current = cjk.get(alt)
                        if current is None or _cjk_rank(city) > _cjk_rank(current):
                            cjk[alt] = city

        return cls(places, cjk, codes)

    # --- lookups ------------------------------------------------------------------

    def _match_at(self, words: list[str], i: int) -> tuple[Place, int] | None:
        """The longest place name starting at words[i], and how many words it spans."""
        for n in range(min(_MAX_NGRAM, len(words) - i), 0, -1):
            gram = words[i : i + n]
            if all(w in _NOT_PLACES for w in gram):
                continue
            place = self._by_name.get(" ".join(gram))
            if place is not None:
                return place, n
        return None

    def find(self, text: str) -> list[Place]:
        """Every place named in a location string, longest names first."""
        text = nfkc(text)
        found: list[Place] = []
        if _CJK.search(text):
            names = sorted((name for name in self._cjk if name in text), key=len, reverse=True)
            # A name inside a longer one is part of it, not a place: 上 (Kami) in 上海, 京都 in 東京都.
            found.extend(self._cjk[name] for name in names if not any(name in longer for longer in names
                                                                      if len(longer) > len(name)))
        for part in _PART_BREAK.split(text):
            tokens = _TOKEN.findall(part)
            words = [t.lower().rstrip(".") for t in tokens]
            i = 0
            while i < len(tokens):
                match = self._match_at(words, i)
                # A capitalized code counts only at the end of a part, as in "Osaka, JP" or "Remote - US":
                # in "AI Engineer" the AI is not Anguilla, and "in" is never India.
                if match is None and i == len(tokens) - 1 and tokens[i].isupper() and 2 <= len(tokens[i]) <= 3:
                    code_place = self._codes.get(tokens[i])
                    match = (code_place, 1) if code_place else None
                if match:
                    found.append(match[0])
                    i += match[1]
                else:
                    i += 1
        return found

    def remove_places(self, text: str) -> str:
        """The text lowercased with every place name taken out, for normalizing job titles.

        Tokens are split on spaces only, so "C++", "C#" and "Level 2" survive intact.
        """
        tokens = nfkc(text).split()
        words = [t.strip(_EDGE_PUNCTUATION).lower() for t in tokens]
        kept: list[str] = []
        i = 0
        while i < len(words):
            if not words[i]:
                i += 1  # a lone dash or bar
                continue
            match = self._match_at(words, i)
            if match:
                i += match[1]
            else:
                kept.append(words[i])
                i += 1
        return " ".join(kept)

    def lookup(self, name: str | None) -> Place | None:
        """The place a single name refers to, such as a model's office_city."""
        if not name:
            return None
        key = nfkc(name).strip().lower()
        if key in self._by_name:
            return self._by_name[key]
        found = self.find(name)
        return found[0] if found else None

    def first_city(self, text: str) -> Place | None:
        places = [p for p in self.find(text) if p.kind == "city"]
        japanese = [p for p in places if p.country == "JP"]
        return (japanese or places or [None])[0]

    def is_tokyo(self, place: Place | None) -> bool:
        return place is not None and place.country == "JP" and place.admin1.lower() == "tokyo"

    def is_kansai(self, place: Place | None) -> bool:
        return place is not None and place.country == "JP" and place.admin1.lower() in KANSAI_PREFECTURES


# --- location verdict ----------------------------------------------------------------

LocationCategory = Literal["japan", "asia", "anywhere", "remote", "outside", "unknown"]


def classify_location(text: str, gaz: Gazetteer) -> LocationCategory:
    text = _EXCLUSION.split(nfkc(text or ""), maxsplit=1)[0].strip()  # only the places the job is open to
    if not text:
        return "unknown"
    places = gaz.find(text)
    words = {w.lower() for w in _TOKEN.findall(text)}
    if any(p.country == "JP" for p in places):
        return "japan"
    if _ASIA.search(text):
        return "asia"
    if words & _ANYWHERE and not (places and _ANYWHERE_IN.search(text)):
        return "anywhere"
    if places:
        return "outside"
    if "remote" in words:
        return "remote"
    return "unknown"


# --- code rules -------------------------------------------------------------------------


@dataclass(frozen=True)
class Removal:
    rule: str
    quote: str


BLACKLISTED = "blacklisted"  # the code rule for an employer on the blacklist (jobfinder.blacklist)


_NON_SOFTWARE = re.compile(
    r"\b(sales|account executive|recruit(er|ing)?|marketing|legal|finance|customer success)\b", re.IGNORECASE
)
_SOFTWARE = re.compile(
    r"\b(engineer(s|ing)?|developer(s)?|programmer(s)?|sre|devops|full[- ]?stack|front[- ]?end|"
    r"back[- ]?end|qa|data engineer)\b",
    re.IGNORECASE,
)


def looks_like_role(text: str) -> bool:
    """Text that names a job, such as 'Backend Engineer' or 'Account Executive'."""
    return bool(_SOFTWARE.search(text) or _NON_SOFTWARE.search(text))


_CONTRACT_FIELD = re.compile(r"contract|freelance|temporary|\btemp\b", re.IGNORECASE)
_PERMANENT_FIELD = re.compile(r"full[- ]?time|permanent|regular", re.IGNORECASE)
_CONTRACT_TITLE = re.compile(r"^\W*(contract|freelance)\b", re.IGNORECASE)
_JAPANESE_CHAR = re.compile(r"[぀-ゟ゠-ヿｦ-ﾝ㐀-䶿一-鿿]")
JAPANESE_SHARE = 0.30


def japanese_share(text: str) -> float:
    letters = sum(1 for ch in text if ch.isalpha())
    if not letters:
        return 0.0
    return len(_JAPANESE_CHAR.findall(text)) / letters


def is_japanese_posting(text: str) -> bool:
    """Written in Japanese, not just mentioning it. Such jobs are the backup list, so they queue last."""
    return bool(text) and japanese_share(text) > JAPANESE_SHARE


def code_rules(
    *, title: str, location: str, description_text: str, fields: dict[str, Any], gaz: Gazetteer
) -> Removal | None:
    """The first code rule that clearly removes this posting, or None."""
    if classify_location(location, gaz) == "outside":
        return Removal("location", location)

    if _NON_SOFTWARE.search(title) and not _SOFTWARE.search(title):
        return Removal("not software", title)

    employment = str(fields.get("employment_type") or "")
    if _CONTRACT_FIELD.search(employment) and not _PERMANENT_FIELD.search(employment):
        return Removal("contract", employment)
    if _CONTRACT_TITLE.search(title):
        return Removal("contract", title)
    return None


_TITLE_STRONG = re.compile(r"\b(react|java|javascript|front[- ]?end|full[- ]?stack|back[- ]?end)\b", re.IGNORECASE)
_TITLE_WEAK = re.compile(r"\b(engineer|developer)\b", re.IGNORECASE)
JAPANESE_POSTING_DROP = 10  # more than the highest English prescore (7), so every English job goes first


def prescore(
    *,
    title: str,
    location: str,
    posted_at: str | None,
    fields: dict[str, Any],
    gaz: Gazetteer,
    profile: Profile,
    rates: dict[str, float],
    description_text: str = "",
) -> float:
    """Queue order for the model: 3 × location + 2 × title + 1 × pay at floor + 1 × posted this week,
    minus JAPANESE_POSTING_DROP for a posting written in Japanese."""
    category = classify_location(location, gaz)
    location_hint = {"japan": 1.0, "asia": 1.0, "anywhere": 1.0, "remote": 0.5}.get(category, 0.0)

    if _TITLE_STRONG.search(title):
        title_hint = 1.0
    elif _TITLE_WEAK.search(title):
        title_hint = 0.5
    else:
        title_hint = 0.0

    pay = score.annual_usd(
        fields.get("pay_min"), fields.get("pay_max"), fields.get("pay_currency"), fields.get("pay_period"), rates
    )
    pay_hint = 1.0 if pay is not None and pay >= profile.pay_floor_usd else 0.0

    age = days_since(posted_at)
    fresh_hint = 1.0 if age is not None and age <= 7 else 0.0

    total = 3 * location_hint + 2 * title_hint + pay_hint + fresh_hint
    return total - JAPANESE_POSTING_DROP if is_japanese_posting(description_text) else total


def days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).total_seconds() / 86400


# --- fact rules -------------------------------------------------------------------------


def fact_rules(
    facts: dict[str, Any], *, title: str, profile: Profile, gaz: Gazetteer, restored: Iterable[str] = ()
) -> Removal | None:
    """The first fact rule that removes this job. "unclear" never removes."""
    skip = set(restored)
    evidence = facts.get("evidence") or {}

    if "Not open to Japan" not in skip and facts.get("open_to_japan_resident") == "no":
        return Removal("Not open to Japan", evidence.get("open_to_japan_resident", ""))

    if "Not payroll" not in skip and facts.get("employment") in ("contract", "freelance"):
        return Removal("Not payroll", evidence.get("employment", ""))

    if "Can't commute" not in skip and not can_commute(facts, profile, gaz):
        return Removal("Can't commute", f"{facts.get('work_mode')} in {facts.get('office_city')}")

    if "Not software" not in skip and facts.get("is_software_job") == "no":
        return Removal("Not software", title)
    return None


def needs_japanese(facts: dict[str, Any], override: bool | None = None) -> bool:
    """Japanese is mandatory, so the job goes to the Japanese list instead of the main one. Asked only
    after fact_rules found nothing, so that list holds jobs that are targets except for the language.
    override is the candidate moving the job by hand, which wins over the model's answer."""
    return override if override is not None else facts.get("japanese") == "required"


def can_commute(facts: dict[str, Any], profile: Profile, gaz: Gazetteer) -> bool:
    """False only when the office is known, outside the commutable list, and not a Tokyo hybrid role."""
    mode = facts.get("work_mode")
    city = facts.get("office_city")
    if mode not in ("onsite", "hybrid") or not city:
        return True
    if is_commutable(city, profile, gaz):
        return True
    return mode == "hybrid" and gaz.is_tokyo(gaz.lookup(city))


def is_commutable(city: str | None, profile: Profile, gaz: Gazetteer) -> bool:
    if not city:
        return False
    if city.strip().lower() in profile.commutable_cities:
        return True
    place = gaz.lookup(city)
    return place is not None and place.name.lower() in profile.commutable_cities


def unclear_facts(facts: dict[str, Any]) -> bool:
    """A fact a removal rule depends on could not be read from the posting."""
    if facts.get("open_to_japan_resident") == "unclear" or facts.get("employment") == "unclear":
        return True
    return facts.get("work_mode") in ("onsite", "hybrid") and not facts.get("office_city")
