"""Shared helpers for bms.py (headed-browser path) and bms_api.py (fast path).

Kept dependency-free so both import paths stay light.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

PLAY_HOME = "https://in.bookmyshow.com"

DATE_RE = re.compile(r"^\d{8}$")
DEFAULT_EVENT_RE = re.compile(r"\b(ET\d{5,})\b", re.I)
URL_RE = re.compile(r"^https?://", re.I)

UA_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


class MonitorError(Exception):
    """The monitor run could not be completed (nav failure, parse failure, ...)."""


def kebab(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


@dataclass
class Resolution:
    name: str          # display name (best effort)
    slug: str          # URL slug for the movie
    et: str            # event code, e.g. ET00518674
    via: str           # "movie-url" | "movie-name" | "et-code" ...
    from_list: str = ""
    candidates: list = field(default_factory=list)


def resolve_from_url(url: str) -> Resolution:
    """Extract (slug, ET code) from any BookMyShow movie/buy-tickets URL."""
    segs = [s for s in urllib.parse.urlparse(url).path.split("/") if s]
    et, slug = None, None
    try:
        bt = next(i for i, s in enumerate(segs) if s.lower() == "buytickets")
        if bt >= 1:
            slug = segs[bt - 1]
        if bt + 1 < len(segs) and DEFAULT_EVENT_RE.fullmatch(segs[bt + 1]):
            et = segs[bt + 1].upper()
    except StopIteration:
        pass
    if et is None:
        for i, s in enumerate(segs):
            if DEFAULT_EVENT_RE.fullmatch(s):
                et = s.upper()
                if i >= 1:
                    slug = segs[i - 1]
                break
    if et is None:
        raise MonitorError(
            "The MOVIE URL doesn't contain an ET code (like ET00518674). "
            "Open the movie's page on bookmyshow.com in your browser and copy that address instead."
        )
    name = (slug or "").replace("-", " ").strip().title() or et
    return Resolution(name=name, slug=(slug or "").lower(), et=et, via="movie-url")


def slug_from_url(url: str) -> str:
    try:
        segs = [s for s in urllib.parse.urlparse(url).path.split("/") if s]
        for i, s in enumerate(segs):
            if DEFAULT_EVENT_RE.fullmatch(s):
                return (segs[i - 1] if i >= 1 else s).lower()
    except Exception:
        pass
    return ""


def build_buy_url(city: str, slug: str, et: str, datecode: str) -> str:
    return f"{PLAY_HOME}/movies/{city}/{slug}/buytickets/{et}/{datecode}"


# ---------------------------------------------------------------------------
# venue-row composition shared by both detection paths
# ---------------------------------------------------------------------------

def show_counts_from_rows(rows: list, target: str) -> dict:
    counts = {}
    for v in rows:
        per = v.get("perDate") or {}
        n = per.get(target, v.get("shows", 0) if not per else 0)
        counts[v["name"]] = n
    return counts


def venue_names_from_rows(rows: list) -> list:
    names: list = []
    for v in rows:
        if v.get("name") and v["name"] not in names:
            names.append(v["name"])
    return names


def venue_dates_from_rows(rows: list) -> dict:
    venue_dates: dict = {}
    for v in rows:
        for d, cnt in sorted((v.get("perDate") or {}).items(), key=lambda kv: str(kv[0])):
            venue_dates.setdefault(str(d), [])
            if v["name"] not in venue_dates[str(d)]:
                venue_dates[str(d)].append(v["name"])
    return venue_dates


def compose_result(
    *,
    source: str,
    resolution: Resolution,
    buy_url: str,
    rows: list,
    target: str,
    date_state: str,
    extras: dict | None = None,
) -> dict:
    """Build the standard result dict that main.py consumes."""
    result = {
        "source": source,
        "date_state": date_state,
        "target_venues": venue_names_from_rows(rows),
        "show_counts": show_counts_from_rows(rows, target),
        "venue_dates": venue_dates_from_rows(rows),
        "movie_display": resolution.name,
        "slug": resolution.slug,
        "et": resolution.et,
        "buy_url": buy_url,
    }
    if extras:
        result.update(extras)
    return result
