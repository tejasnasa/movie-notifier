"""BookMyShow showtimes monitor (Playwright, headed).

How detection works (verified Oct 2026):
- The buy-tickets URL `/movies/{city}/{slug}/buytickets/{etcode}/{yyyymmdd}`
  server-side-renders the whole showtimes payload into
  `window.__INITIAL_STATE__.showtimesFunctionalApi` (an RTK Query cache).
- A `fetchPrimaryDynamic-<et>---<yyyymmdd>-<region>` cache entry in it contains
  the full venue list for that day: data.data.showtimeWidgets[] where
  type=="groupList" -> data[].data[] items of type "venue-card", each with
  additionalData.venueName / venueCode / showtimesSections[].showtimes[] hills
  (each chip has additionalData.showDateCode / showTime / availStatus).
- BookMyShow's Cloudflare hard-blocks headless Chromium (403), so we run HEADED
  (on GitHub Actions: inside Xvfb). Debugging: run main.py --dump to save the
  extracted state + page text to debug/.

Fallbacks (kept in case BMS switches to a client-side fetch):
- intercept `/movies-data/v5/showtimes-by-event/primary-dynamic` responses from
  the page's own traffic and parse them generically;
- read DOM text for the "no shows available in this region" empty state.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

import bms_api
from bms_common import (
    DATE_RE,
    DEFAULT_EVENT_RE,
    MonitorError,
    PLAY_HOME,
    Resolution,
    URL_RE,
    build_buy_url,
    compose_result,
    resolve_from_url,
    slug_from_url,
)

PRIMARY_MARK = "showtimes-by-event/primary-dynamic"
CHALLENGE_TITLES = ("just a moment", "attention required")

STATE_WAIT_MILLIS = 26_000        # max wait for the SSR state to settle
STATE_EXTRA_MILLIS = 4_000        # grace period after first sight of venues

# Extracts every RTK Query cache entry with a payload. Runs inside the browser.
EXTRACT_STATE_JS = r"""
() => {
  const rb = (window.__INITIAL_STATE__ || {}).showtimesFunctionalApi;
  const queries = (rb && rb.queries) || {};
  const out = [];
  for (const [key, qobj] of Object.entries(queries)) {
    if (!qobj || qobj.isUninitialized) continue;
    const payload = qobj.data;
    if (!payload) continue;
    const args = qobj.originalArgs || {};
    const body = (payload.data && typeof payload.data === 'object') ? payload.data : payload;
    const venues = [];
    for (const w of (body.showtimeWidgets || [])) {
      if (w && w.type !== 'groupList') continue;
      for (const g of ((w && w.data) || [])) {
        for (const it of ((g && g.data) || [])) {
          if (!it || it.type !== 'venue-card') continue;
          const ad = it.additionalData || {};
          let showCount = 0;
          const perDate = {};
          for (const sec of (it.showtimesSections || [])) {
            for (const chip of ((sec && sec.showtimes) || [])) {
              showCount += 1;
              const dc = (chip && chip.additionalData && chip.additionalData.showDateCode) || '';
              perDate[dc] = (perDate[dc] || 0) + 1;
            }
          }
          venues.push({
            name: ad.venueName || ad.regionalName || '',
            code: ad.venueCode || '',
            shows: showCount,
            perDate: perDate,
          });
        }
      }
    }
    out.push({
      key: key,
      endpointName: qobj.endpointName || '',
      status: String(qobj.status || ''),
      isError: !!qobj.isError,
      etCodes: String(args.etCodes || ''),
      argsDate: String(args.dateCode || ''),
      regionCode: String(args.regionCode || ''),
      bodyDate: String((body.additionalData && body.additionalData.dateCode) || ''),
      eventCode: String((body.additionalData && body.additionalData.eventCode) || ''),
      venueCount: venues.length,
      venues: venues,
    });
  }
  return out;
}
"""


class MonitorError(Exception):
    """The monitor run could not be completed (nav failure, parse failure, ...)."""


@dataclass
class Resolution:
    name: str          # display name (best effort)
    slug: str          # URL slug for the movie
    et: str            # event code, e.g. ET00518674
    via: str           # "movie-url" | "et-code" | "movie-name"
    from_list: str = ""
    candidates: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# generic payload parsing (fallback for directly-intercepted API responses)
# ---------------------------------------------------------------------------

DATE_KEYS = {"datecode", "eventdate", "eventdatecode", "showdate", "showdatecode", "date", "eventdatestring"}
VENUE_IDENT_KEYS = ("venuecode", "venueid", "theatrecodenew")
VENUE_NAME_KEYS = ("venuename", "venuenamestr", "venuenamedyn", "theatrename", "theatername", "venuedisplayname", "regionalname")
SHOW_KEYS_RE = re.compile(r"showtime|session|timing|show", re.I)

NODE_BUDGET = 400_000  # safety valve so a huge/unexpected payload can't hang us


def _norm_date(v) -> str | None:
    """Best-effort normalize a value to YYYYMMDD; None if it isn't a date."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        if n >= 1e12:  # ms timestamp
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc).strftime("%Y%m%d")
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if DATE_RE.match(s):
        return s
    if s.isdigit():
        return _norm_date(int(s))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    return None


def _generic_parse(payloads: list) -> list:
    """Walk arbitrary JSON shapes for venue-ish objects; returns qrow-shaped rows.

    Used only when the SSR state is missing but the page's own network traffic
    still carried a `primary-dynamic` response we could capture.
    """
    per_date: dict[str, dict] = {}     # date(str|'') -> (code, name) -> row
    dates_seen: set = set()
    budget = [NODE_BUDGET]

    def add(name, code, date, count):
        bucket = per_date.setdefault(date, {})
        k = (code, name)
        row = bucket.setdefault(k, {"name": name, "code": code, "shows": 0, "perDate": {}})
        row["shows"] = max(row["shows"], count or 0)

    def walk(node, date):
        budget[0] -= 1
        if budget[0] <= 0:
            return
        if isinstance(node, dict):
            local = date
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in DATE_KEYS:
                    nd = _norm_date(v)
                    if nd:
                        local = nd
                        dates_seen.add(nd)
            lower = {(k.lower() if isinstance(k, str) else k): v for k, v in node.items()}
            if lower.get("type") == "venue-card":
                ad = lower.get("additionaldata") if isinstance(lower.get("additionaldata"), dict) else {}
                name = ad.get("venueName") or _venue_name(lower)
            elif any(k in lower for k in VENUE_IDENT_KEYS) or any(k in lower for k in VENUE_NAME_KEYS):
                name = _venue_name(lower)
                ad = {}
            else:
                name = None
                ad = {}
            if name:
                shows = (ad.get("showtimessections") or [])
                count = 0
                for sec in shows if isinstance(shows, list) else []:
                    st = (sec or {}).get("showtimes") if isinstance(sec, dict) else None
                    if isinstance(st, list):
                        count += len(st)
                if count == 0:
                    count = len(lower.get("showtimes")) if isinstance(lower.get("showtimes"), list) else 0
                add(name, str(ad.get("venueCode") or lower.get("venuecode") or lower.get("venueid") or ""), local, count)
            for k, v in node.items():
                nxt = local
                if isinstance(k, str) and DATE_RE.match(str(k)) and isinstance(v, (dict, list)):
                    dates_seen.add(str(k))
                    nxt = str(k)
                walk(v, nxt)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, date)

    for pl in payloads:
        walk(pl.get("body"), pl.get("datecode") or None)

    rows: list = []
    for date in sorted(per_date, key=lambda d: "" if d is None else d):
        for (_code, _name), row in per_date[date].items():
            if row["perDate"]:
                continue
            if date:
                row["perDate"] = {date: row["shows"]}
            rows.append(row)
    grouped = [{"name": r["name"], "code": r["code"], "shows": r["shows"], "perDate": r["perDate"]} for r in rows]
    if not grouped:
        return []
    return [{"key": "api-fallback", "endpointName": "fetchPrimaryDynamic", "venues": grouped}]


def _venue_name(lower: dict) -> str | None:
    for k in VENUE_NAME_KEYS:
        v = lower.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:140]
        if isinstance(v, (int, float)):
            return str(v)
    return None


# ---------------------------------------------------------------------------
# navigation helpers
# ---------------------------------------------------------------------------

def _looks_like_challenge(page) -> bool:
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    return any(t in title for t in CHALLENGE_TITLES)


def _query_date(url: str) -> str | None:
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except Exception:
        return None
    for k, vals in q.items():
        if k.lower() in ("datecode", "date", "eventdate") and vals:
            return _norm_date(vals[0])
    return None


def _body_text(page, limit: int = 4000) -> str:
    try:
        return page.locator("body").inner_text(timeout=10_000)[:limit]
    except Exception:
        try:
            return (page.content() or "")[:limit]
        except Exception:
            return ""


def _no_shows_dom(text: str) -> bool:
    t = (text or "").lower()
    return any(
        m in t
        for m in ("no shows available", "currently no shows", "no shows for this", "no shows found")
    )


# ---------------------------------------------------------------------------
# movie resolution (movie URL / ET code / name)
# ---------------------------------------------------------------------------

def _jsonld_candidates(page) -> list[dict]:
    try:
        texts = page.eval_on_selector_all(
            'script[type="application/ld+json"]',
            "els => els.map(e => e.textContent)",
        )
    except Exception:
        return []
    items: list[dict] = []
    stack: list = [t for t in (texts or []) if t]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            try:
                stack.append(json.loads(node))
            except Exception:
                continue
        elif isinstance(node, dict):
            el = node.get("itemListElement")
            if isinstance(el, list):
                items.extend(it for it in el if isinstance(it, dict))
            stack.extend(v for v in node.values() if isinstance(v, (dict, list, str)))
        elif isinstance(node, list):
            stack.extend(x for x in node if isinstance(x, (dict, list, str)))
    flat: list[dict] = []
    for it in items:
        inner = it.get("item") if isinstance(it.get("item"), dict) else {}
        flat.append(
            {
                "name": str(it.get("name") or inner.get("name") or "").strip(),
                "url": str(it.get("url") or inner.get("url") or "").strip(),
            }
        )
    return flat


def search_explore(page, city: str, log) -> tuple[list[Resolution], list[dict]]:
    """Scan the SSRed explore pages' JSON-LD. Returns (candidates, raw_items)."""
    cands: list[Resolution] = []
    raw: list[dict] = []
    for exp in (f"{PLAY_HOME}/explore/movies-{city}", f"{PLAY_HOME}/explore/upcoming-movies-{city}"):
        try:
            page.goto(exp, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:
            log(f"explore page failed ({exp}): {type(exc).__name__}")
            continue
        if _looks_like_challenge(page):
            page.wait_for_timeout(20_000)
            try:
                page.goto(exp, wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                continue
        page_items = _jsonld_candidates(page)
        raw.extend(page_items)
        for it in page_items:
            url = it.get("url") or ""
            m = DEFAULT_EVENT_RE.search(url)
            if not m:
                continue
            etc = m.group(1).upper()
            if any(c.et == etc for c in cands):
                continue
            name = it.get("name") or etc
            cands.append(
                Resolution(
                    name=name,
                    slug=slug_from_url(url) or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
                    et=etc,
                    via="explore-list",
                    from_list=exp.rsplit("/", 1)[-1],
                )
            )
    cands.sort(key=lambda c: (0 if c.from_list.startswith("movies-") else 1, c.et))
    return cands, raw


def resolve_movie(page, cfg: dict, log) -> Resolution:
    raw = str(cfg["movie"]).strip()

    if URL_RE.match(raw):
        return resolve_from_url(raw)

    explorers, raw_items = search_explore(page, cfg["city"], log)

    m = DEFAULT_EVENT_RE.fullmatch(raw)
    if m:
        et = m.group(1).upper()
        for c in explorers:
            if c.et == et:
                return Resolution(
                    name=c.name, slug=c.slug, et=et, via="et-code",
                    from_list=c.from_list, candidates=[x.name for x in explorers[:15]],
                )
        raise MonitorError(
            f"ET code {et} was not found on the explore pages for city '{cfg['city']}'. "
            "Set MOVIE to the movie's full BookMyShow URL instead (most reliable)."
        )

    needle = raw.lower()
    kebab = re.sub(r"[^a-z0-9]+", "-", needle).strip("-")
    exact = [c for c in explorers if c.name.lower() == needle]
    slugm = [c for c in explorers if c.slug == kebab]
    contains = [c for c in explorers if needle in c.name.lower()]
    pick = exact or slugm or contains
    if not pick:
        sample = ", ".join(c.name for c in explorers[:10]) or "(no JSON-LD found)"
        raise MonitorError(
            f"Movie {raw!r} not found in BookMyShow's now-showing/upcoming lists for '{cfg['city']}' "
            f"(saw: {sample}). Set MOVIE to the movie's full BookMyShow URL instead (most reliable)."
        )
    return Resolution(
        name=pick[0].name, slug=pick[0].slug, et=pick[0].et, via="movie-name",
        from_list=pick[0].from_list, candidates=[c.name for c in explorers[:15]],
    )


# ---------------------------------------------------------------------------
# main monitor run
# ---------------------------------------------------------------------------

def _pick_state_query(qrows: list, target: str) -> tuple[dict | None, str]:
    """Choose the fetchPrimaryDynamic row best matching the target date."""
    primaries = [q for q in qrows if q.get("endpointName") == "fetchPrimaryDynamic" and not q.get("isError")]
    if not primaries:
        return None, "primaries:$NONE"
    for q in primaries:
        if q.get("argsDate") == target or q.get("bodyDate") == target:
            return q, "on-date"
    if len(primaries) == 1:
        q = primaries[0]
        return q, f"sole-date:{q.get('argsDate') or q.get('bodyDate') or '?'}"
    return primaries[0], "mismatch:" + ",".join(
        sorted({(q.get("argsDate") or q.get("bodyDate") or "?") for q in primaries})
    )


def _result_from_state_row(cfg, resolution, buy_url, qrow: dict, state_txt: str) -> dict:
    return compose_result(
        source="state",
        resolution=resolution,
        buy_url=buy_url,
        rows=qrow["venues"],
        target=cfg["datecode"],
        date_state=state_txt,
        extras={
            "query_key": qrow.get("key", ""),
            "region_code": qrow.get("regionCode", ""),
        },
    )


def _empty_result(cfg, resolution, buy_url, source: str, note: dict | None) -> dict:
    return {
        "source": source,
        "date_state": "on-date",
        "target_venues": [],
        "show_counts": {},
        "venue_dates": {},
        "note": note or {},
        "movie_display": resolution.name,
        "slug": resolution.slug,
        "et": resolution.et,
        "buy_url": buy_url,
    }


def _dump_result(dump_dir: Path | None, result: dict) -> None:
    if dump_dir is None:
        return
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / "outcome.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception:
        pass


def run_monitor(cfg: dict, *, headless: bool, dump_dir: Path | None = None, log=print) -> dict:
    """Answer the question 'are tickets open?' for cfg.

    Strategy:
      1. fast path (bms_api): direct HTTPS call to BookMyShow's own showtimes
         API with a faked full browser-fingerprint header set. No browser, ~2s.
         Only usable for MOVIE = URL or ET code.
      2. headed-browser SSR path (this function's rest): resolves movies by
         name via the explore pages, opens the buy-tickets page and reads the
         SSR-embedded RTK showtimes state. Slower (~15-30s) but robust.

    Returns a dict with: source ("api" | "state" | "dom"), target_venues,
    show_counts, movie_display, slug, et, buy_url, date_state. Raises
    MonitorError only when EVERY path fails to produce a trustworthy answer.
    """
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)

    try:
        fast = bms_api.run_monitor_fast(cfg, log=log, dump_dir=dump_dir)
    except Exception as exc:
        log(f"fast path skipped ({type(exc).__name__}: {exc} — using the headed browser)")
        fast = None
    if fast is not None:
        return fast

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        try:
            ctx = browser.new_context(
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1400, "height": 900},
            )
            page = ctx.new_page()

            if headless:
                # keep a realistic UA even if someone forces headless locally
                ua = page.evaluate("navigator.userAgent")
                better = ua.replace("HeadlessChrome", "Chrome")
                if better != ua:
                    ctx.close()
                    ctx = browser.new_context(
                        user_agent=better,
                        locale="en-IN",
                        timezone_id="Asia/Kolkata",
                        viewport={"width": 1400, "height": 900},
                    )
                    page = ctx.new_page()

            # --- resolve the movie (URL / ET code / name) -----------------------
            try:
                resolution = resolve_movie(page, cfg, log)
            except MonitorError:
                raise
            except Exception as exc:
                raise MonitorError(f"movie resolution failed: {type(exc).__name__}: {exc}") from exc
            log(
                f"resolved via {resolution.via}: name={resolution.name!r} et={resolution.et} "
                f"slug={resolution.slug!r}"
            )
            if dump_dir is not None:
                (dump_dir / "resolved.json").write_text(
                    json.dumps(
                        {
                            "name": resolution.name,
                            "slug": resolution.slug,
                            "et": resolution.et,
                            "via": resolution.via,
                            "from_list": resolution.from_list,
                            "candidates": resolution.candidates,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            buy_url = build_buy_url(cfg["city"], resolution.slug, resolution.et, cfg["datecode"])
            log(f"opening {buy_url}")

            # network capture (fallback source)
            captured: list = []
            page.on("response", lambda r: captured.append(r) if PRIMARY_MARK in r.url else None)

            for attempt in (1, 2):
                try:
                    page.goto(buy_url, wait_until="domcontentloaded", timeout=45_000)
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise MonitorError(f"failed to load the buy-tickets page: {exc}") from exc
                    if _looks_like_challenge(page):
                        log("cloudflare challenge detected; waiting 30s and retrying")
                        page.wait_for_timeout(30_000)
                    else:
                        page.wait_for_timeout(10_000)

            if _looks_like_challenge(page):
                page.wait_for_timeout(30_000)
                if _looks_like_challenge(page):
                    raise MonitorError("BookMyShow kept showing a Cloudflare challenge; try again later.")

            # --- primary source: SSR RTK state ----------------------------------
            saw_venues_at: int | None = None
            qrows: list = []
            deadline_ms = STATE_WAIT_MILLIS
            elapsed = 0
            while elapsed <= deadline_ms:
                try:
                    qrows = page.evaluate(EXTRACT_STATE_JS)
                except Exception as exc:
                    log(f"state extraction hiccup: {type(exc).__name__}")
                    qrows = []
                primaries = [q for q in qrows if q.get("endpointName") == "fetchPrimaryDynamic" and not q.get("isError")]
                if primaries and primaries[0].get("venueCount", 0) > 0:
                    if saw_venues_at is None:
                        saw_venues_at = elapsed
                    elif elapsed - saw_venues_at >= STATE_EXTRA_MILLIS:
                        break
                elif primaries and primaries[0].get("venueCount", 0) == 0:
                    # zero venues for a fetched primary query: one more look to be
                    # sure it's not a mid-hydration artifact
                    page.wait_for_timeout(3_000)
                    elapsed += 3_000
                    try:
                        qrows = page.evaluate(EXTRACT_STATE_JS)
                    except Exception:
                        pass
                    break
                page.wait_for_timeout(1_000)
                elapsed += 1_000

            if dump_dir is not None:
                (dump_dir / "state_summary.json").write_text(
                    json.dumps(qrows, indent=2), encoding="utf-8"
                )

            primary = [q for q in qrows if q.get("endpointName") == "fetchPrimaryDynamic"]
            if primary:
                all_error = all(q.get("isError") for q in primary)
                if all_error:
                    raise MonitorError(
                        "BookMyShow's showtimes API returned an error inside the page (isError=true). "
                        "This often means the site is briefly broken; it may recover."
                    )
                qrow, state_txt = _pick_state_query(qrows, cfg["datecode"])
                if qrow is not None:
                    if qrow.get("venueCount", 0) > 0:
                        result = _result_from_state_row(cfg, resolution, buy_url, qrow, state_txt)
                        _dump_result(dump_dir, result)
                        return result
                    rows_above = [q for q in primary if q.get("venueCount", 0) > 0]
                    if rows_above:
                        # prefer a row that has venues even if date picks missed
                        result = _result_from_state_row(cfg, resolution, buy_url, rows_above[0], "fallback-row")
                        _dump_result(dump_dir, result)
                        return result
                    log("primary-dynamic loaded with zero venues for this date")
                    result = _empty_result(
                        cfg, resolution, buy_url, "state",
                        {"note": "primary-dynamic fetched; venue count = 0", "query_key": qrow.get("key")},
                    )
                    _dump_result(dump_dir, result)
                    return result

            # --- fallback: intercepted primary-dynamic responses ----------------
            payloads: list[dict] = []
            for r in captured:
                try:
                    body = r.json()
                except Exception:
                    continue
                payloads.append({"url": r.url, "datecode": _query_date(r.url), "body": body})
            if dump_dir is not None:
                for i, pl in enumerate(payloads):
                    (dump_dir / f"primary_dynamic_{i:02d}.json").write_text(
                        json.dumps(pl["body"], indent=2, default=str), encoding="utf-8"
                    )
            if payloads:
                parsed_rows = [_g for _g in _generic_parse(payloads)]
                for qrow in parsed_rows:
                    if qrow["venues"]:
                        result = _result_from_state_row(cfg, resolution, buy_url, qrow, "api")
                        result["source"] = "api"
                        _dump_result(dump_dir, result)
                        return result

            # --- last resort: DOM text ------------------------------------------
            text = _body_text(page)
            if dump_dir is not None:
                (dump_dir / "page_text.txt").write_text(text, encoding="utf-8")
                _dump_result(dump_dir, {"note": "dom-fallback", "url": page.url})
            if _no_shows_dom(text):
                return _empty_result(cfg, resolution, buy_url, "dom", {"note": "no-shows empty state in DOM"})
            snippet = " ".join(text.split())[:300]
            raise MonitorError(
                "no showtimes data could be read (no state, no API response, no DOM marker) "
                f"— page said: {snippet!r}"
            )
        finally:
            browser.close()
