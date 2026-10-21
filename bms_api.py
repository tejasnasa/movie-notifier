"""BookMyShow showtimes API — direct "fast path" (no browser).

Calls the same showtimes endpoint the BookMyShow page itself uses, from a
plain HTTPS client. This works because the request fakes the entire
browser-fingerprint signal set the site's bot filter scores (Chrome UA +
sec-ch-ua client hints + Referer + BookMyShow's own x-* region/app headers).

Verified Oct 2026 from a plain Python client (200 + full venue JSON, 89
venues): the API endpoint passes where HTML pages (/explore/...) and bare
curl get 403-challenged.

Caveats:
- Cloudflare's rule set may tighten at any time (e.g., require the real JS
  challenge). Every failure here falls back to the headed-browser SSR method
  in bms.py — this module raises FastPathError and never lies about data.
- Region resolution uses BookMyShow's public regions.json (bmscdn CDN). If
  the city isn't found there (or the file can't be fetched), the fast path
  refuses entirely — a guessed region code could silently query the wrong
  city, and a false "tickets are open" is worse than a slower browser run.
- Only URL / ET-code MOVIE inputs use the fast path: name-based resolution
  needs the explore HTML pages, which plain clients can't fetch.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from bms_common import (
    DEFAULT_EVENT_RE,
    PLAY_HOME,
    Resolution,
    UA_CHROME,
    build_buy_url,
    compose_result,
    format_variants_from_body,
    kebab,
    resolve_from_url,
)

API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic"
)
REGIONS_URL = "https://in.bmscdn.com/discovery-catalog/response-cache/regions.json"

HINTS = '"Chromium";v="145", "Not:A-Brand";v="99"'

_UA_TRIPLE = (
    # (User-Agent, sec-ch-ua brand list, sec-ch-ua-platform)
    (UA_CHROME, HINTS, '"macOS"'),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        HINTS,
        '"Windows"',
    ),
)


class FastPathError(Exception):
    """Fast path can't do its job; caller should use the headed-browser path."""


@dataclass
class RegionInfo:
    code: str
    slug: str
    lat: str
    lon: str
    geohash: str
    trusted: bool       # True = came from BookMyShow's own regions.json


def _guess_region(city: str) -> RegionInfo:
    return RegionInfo(code=(city.upper()[:6] or "UNKNOWN"), slug=city, lat="", lon="", geohash="", trusted=False)


_SKIPPED_THIS_PROCESS = False


def _fastpath_disabled() -> bool:
    if _SKIPPED_THIS_PROCESS:
        return True
    return os.getenv("BMS_FASTPATH", "").strip().lower() in ("false", "0", "no", "off")


def _skip_rest_of_process() -> None:
    global _SKIPPED_THIS_PROCESS
    _SKIPPED_THIS_PROCESS = True


# ---------------------------------------------------------------------------
# region resolution
# ---------------------------------------------------------------------------

def load_region(city: str, *, timeout: float = 15.0) -> RegionInfo:
    """slug -> RegionCode/lat/long/geohash from BMS's public regions.json."""
    req = urllib.request.Request(
        REGIONS_URL,
        headers={"User-Agent": UA_CHROME, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8-sig", "replace")  # bmscdn sends a BOM
        data = json.loads(raw)
    except Exception:
        return _guess_region(city)
    for tc in (data.get("BookMyShow", {}) or {}).get("TopCities", []) or []:
        slugs = {(tc.get("RegionSlug") or "").lower()}
        for a in tc.get("Alias") or []:
            slugs.add(str(a).split(":")[0].strip().lower())
        if city in slugs:
            return RegionInfo(
                code=tc.get("RegionCode") or city.upper()[:6],
                slug=tc.get("RegionSlug") or city,
                lat=str(tc.get("Lat") or ""),
                lon=str(tc.get("Long") or ""),
                geohash=str(tc.get("GeoHash") or ""),
                trusted=True,
            )
    return _guess_region(city)


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def _headers(ri: RegionInfo, ua_pair) -> dict:
    ua, brands, platform = ua_pair
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{PLAY_HOME}/movies/{ri.slug}/buytickets/",
        "sec-ch-ua": brands,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        "x-app-code": "WEB",
        "x-region-code": ri.code,
        "x-region-slug": ri.slug,
        "x-geohash": ri.geohash,
        "x-latitude": ri.lat or "0",
        "x-longitude": ri.lon or "0",
        "x-location-selection": "manual",
        "x-lsid": "",
    }


def fetch_primary_dynamic(et: str, datecode: str, ri: RegionInfo, *, timeout: float = 20.0) -> dict:
    """GET the primary-dynamic showtimes payload. Raises FastPathError on trouble."""
    params = {
        "eventCode": et,
        "dateCode": datecode,
        "isDesktop": "true",
        "regionCode": ri.code,
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "lat": ri.lat or "0",
        "lon": ri.lon or "0",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    last = ""
    for ua_pair in _UA_TRIPLE:
        req = urllib.request.Request(url, headers=_headers(ri, ua_pair), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError as exc:
                raise FastPathError(f"API returned non-JSON body ({len(raw)} bytes): {exc}") from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            last = f"API HTTP {exc.code} ({exc.reason}) {detail[:80]}"
            if exc.code == 403:
                continue  # bot signature rejected; rotate to the next UA pair
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            last = f"API network error: {exc}"
            continue
    _skip_rest_of_process()
    raise FastPathError(last or "API unreachable")


# ---------------------------------------------------------------------------
# response parsing (v4 shape; tolerates showtimesSections/SSR shapes too)
# ---------------------------------------------------------------------------

def _row_from_card(card: dict) -> dict | None:
    ad = card.get("additionalData") or {}
    name = ad.get("venueName") or ad.get("regionalName") or ""
    if not name:
        return None
    chips = list(card.get("showtimes") or [])
    if not chips:
        for sec in (card.get("showtimesSections") or []):
            chips.extend(sec.get("showtimes") or [])
    per_date: dict = {}
    for chip in chips:
        sa = chip.get("additionalData") or {}
        dc = str(sa.get("showDateCode") or sa.get("dateCode") or "")
        per_date[dc] = per_date.get(dc, 0) + 1
    return {
        "name": str(name)[:140],
        "code": str(ad.get("venueCode") or ""),
        "shows": len(chips),
        "perDate": per_date,
    }


def parse_body(body: dict) -> dict:
    """Extract rows/display name/body date/date-strip styles from the payload."""
    top = body.get("data") if isinstance(body, dict) else None
    if not isinstance(top, dict):
        raise FastPathError("unexpected API payload shape (no data block)")

    rows: list = []
    for w in (top.get("showtimeWidgets") or []):
        if w.get("type") != "groupList":
            continue
        for g in (w.get("data") or []):
            if g.get("type") not in ("venueGroup", "groupList", None):
                continue
            for card in (g.get("data") or []):
                if card.get("type") != "venue-card":
                    continue
                row = _row_from_card(card)
                if row:
                    rows.append(row)

    additional = top.get("additionalData") or {}
    title = (top.get("header") or {}).get("title") or {}
    display = str(title.get("text") or "") if isinstance(title, dict) else ""

    date_styles: dict = {}
    for w in (top.get("topStickyWidgets") or []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in (w.get("data") or []):
            if getattr(item, "get", lambda *_: None) and item.get("id"):
                date_styles[str(item["id"])] = str(item.get("styleId") or "")

    return {
        "rows": rows,
        "display_name": display,
        "body_date": str(additional.get("dateCode") or ""),
        "body_et": str(additional.get("eventCode") or ""),
        "date_styles": date_styles,
    }

MAX_VARIANTS = 8  # base plus at most this many extra format event codes


def _merged_rows(variant_rows: list[dict]) -> tuple[list, dict]:
    """Union venue rows across all format variants of the movie.

    Same venue can sell several formats (2D + IMAX): shows are summed per
    date, format labels collected. Returns (merged rows, venue->formats map).
    """
    order: list[str] = []
    merged: dict[str, dict] = {}
    for vrow, _vlabel in variant_rows:
        for v in vrow:
            key = (v.get("code") or v["name"]).strip().upper() or v["name"]
            m = merged.get(key)
            if not m:
                merged[key] = {
                    "name": v["name"],
                    "code": v.get("code", ""),
                    "shows": int(v.get("shows", 0) or 0),
                    "perDate": dict(v.get("perDate") or {}),
                    "formats": [_vlabel] if _vlabel else [],
                }
                order.append(key)
                continue
            m["shows"] += int(v.get("shows", 0) or 0)
            for d, c in (v.get("perDate") or {}).items():
                m["perDate"][d] = m["perDate"].get(d, 0) + int(c)
            if _vlabel and _vlabel not in m["formats"]:
                m["formats"].append(_vlabel)
    return [merged[k] for k in order], {m["name"]: m["formats"] for m in merged.values()}


def fetch_event_and_rows(et: str, datecode: str, ri: RegionInfo) -> tuple[list, dict, dict]:
    """Fetch one event code's primary-dynamic payload and row-parse it.

    Returns (venue rows, parsed summary, raw body). The parsed summary also
    carries ['format_variants'] — the (etcode, format label) pairs advertised
    by this payload's format-selector.
    """
    body = fetch_primary_dynamic(et, datecode, ri)
    parsed = parse_body(body)
    if parsed["body_et"] and parsed["body_et"].upper() != et:
        raise FastPathError(
            f"API returned a different event ({parsed['body_et']}) than requested ({et})"
        )
    parsed["format_variants"] = format_variants_from_body(body, et)
    return parsed["rows"], parsed, body


# ---------------------------------------------------------------------------
# top-level fast-path run
# ---------------------------------------------------------------------------

def run_monitor_fast(cfg: dict, log=print, dump_dir=None) -> dict | None:
    """Answer with zero browser usage when possible.

    Returns a result dict (main.py contract) or None to signal 'use bms.py'
    (the headed-browser path). Errors surface as FastPathError.
    """
    if _fastpath_disabled():
        raise FastPathError("fast path disabled via BMS_FASTPATH")

    movie = str(cfg["movie"]).strip()
    if re.match(r"^https?://", movie, re.I):
        resolution = resolve_from_url(movie)
    elif DEFAULT_EVENT_RE.fullmatch(movie):
        et = DEFAULT_EVENT_RE.fullmatch(movie).group(1).upper()
        resolution = Resolution(name=movie, slug="", et=et, via="et-code")
    else:
        raise FastPathError("movie name needs the headed-browser resolver (explore pages)")

    et = resolution.et
    ri = load_region(cfg["city"])
    log(f"fast path: et={et} region={ri.code} (trusted={ri.trusted})")
    if not ri.trusted:
        # A guessed region code could silently query the wrong city — both a
        # false positive (wrong-city venues) and a false negative (empty for a
        # valid-but-misguished region) are possible. The browser path resolves
        # the region from the live page instead.
        raise FastPathError(f"city '{cfg['city']}' not in regions.json — falling back")

    base_rows, parsed, base_body = fetch_event_and_rows(et, cfg["datecode"], ri)

    if dump_dir is not None:
        try:
            dump_dir.mkdir(parents=True, exist_ok=True)
            (dump_dir / "primary_dynamic_v4.json").write_text(
                json.dumps(base_body, indent=2, default=str), encoding="utf-8"
            )
        except Exception:
            pass

    # --- expand format variants ------------------------------------------
    # BookMyShow tickets IMAX 2D / 4DX / other-language formats under their own
    # event codes; a venue selling only a variant never shows up in the base
    # venue list (e.g. PVR IMAX Priya Delhi sells Street Fighter IMAX under
    # ET00522122 while the base 2D code lists 31 other cinemas).
    all_rows: list[tuple[list, str]] = [(base_rows, "")]
    variant_fetch_errors: list[str] = []
    tried: list[str] = []
    for vcode, vlabel in parsed["format_variants"]:
        if vcode == et or vcode in tried or len(tried) >= MAX_VARIANTS:
            continue
        try:
            vrows, _vparsed, _vbody = fetch_event_and_rows(vcode, cfg["datecode"], ri)
            tried.append(vcode)
            all_rows.append((vrows, vlabel))
            log(f"fast path: variant {vcode} ({vlabel or 'other format'}): {len(vrows)} venues")
        except Exception as exc:  # noqa: BLE001 — a variant failing must not kill the run
            variant_fetch_errors.append(f"{vcode}: {exc}")
            log(f"fast path: variant {vcode} fetch failed ({exc}) — continuing")

    rows, venue_formats = _merged_rows(all_rows)
    ets_used = [et] + list(tried)

    if not resolution.slug:
        # ET-code input: take the real movie title from the payload; slug for
        # the book link is a best-effort kebab of it.
        name = parsed["display_name"] or resolution.name
        resolution = Resolution(name=name, slug=kebab(name), et=et, via="et-code")
    elif parsed["display_name"]:
        resolution = Resolution(
            name=parsed["display_name"], slug=resolution.slug, et=et, via="movie-url"
        )

    buy_url = build_buy_url(cfg["city"], resolution.slug, et, cfg["datecode"])
    state = "on-date" if parsed["body_date"] in ("", cfg["datecode"]) else f"sole-date:{parsed['body_date']}"
    extras = {
        "query_key": f"api-v4:{','.join(ets_used)}:{cfg['datecode']}:{ri.code}",
        "region_code": ri.code,
        "region_trusted": ri.trusted,
        "date_styles": parsed["date_styles"],
    "variant_ets": list(tried),
        "venue_formats": venue_formats,
        "variant_fetch_errors": variant_fetch_errors,
    }

    if rows:
        return compose_result(
            source="api", resolution=resolution, buy_url=buy_url, rows=rows,
            target=cfg["datecode"], date_state=state, extras=extras,
        )

    log("fast path: zero venues across base + all format variants; region canonical — trustworthy")
    return compose_result(
        source="api", resolution=resolution, buy_url=buy_url, rows=[],
        target=cfg["datecode"], date_state="on-date", extras=extras,
    )
