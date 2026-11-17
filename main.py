#!/usr/bin/env python3
"""movie-notifier — watch BookMyShow for ticket availability and email an alert.

What to watch is HARD-CODED in TARGETS below (movies × dates × theaters).
Only the email settings come from the environment (RESEND_API_KEY, EMAIL_FROM,
EMAIL_TO — GitHub Actions secrets/variables, or a .env file locally).

State is kept in state.json and committed back to the repo on GitHub Actions,
so every venue is alerted exactly once.

Usage:
  python main.py             # normal run
  python main.py --dry-run   # detect only; never send email or change state
  python main.py --force     # re-send alerts even if already notified
  python main.py --dump      # save raw API payloads to debug/ for troubleshooting
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bms import BrowserSession, run_monitor
from bms_common import CloudflareBlocked
from resend_email import send_email
from state_store import git_commit_file, heartbeat_due, load_state, save_state

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# WHAT TO WATCH (hard-coded)
# ---------------------------------------------------------------------------

CITY = "delhi"

# Theater rules: (label, venue-name substring, required screen format or None).
# Venue names as BookMyShow lists them (Delhi/NCR):
#   "PVR IMAX with Laser, Priya: Delhi"
#   "PVR: Cinemagic, Unity One Elegante, NSP, Pitampura"
#   "Cinepolis: Pacific NSP2, Delhi"
PRIYA = ("IMAX Priya", "priya", None)
ELEGANTE = ("NSP Elegante", "elegante", None)
ELEGANTE_PXL = ("NSP Elegante (PXL)", "elegante", "pxl")
PACIFIC_NSP = ("NSP Pacific", "pacific nsp", None)


def _days(start: str, end: str) -> list[str]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


TARGETS = [
    {
        "name": "Godzilla Minus Zero",
        "url": "https://in.bookmyshow.com/movies/delhi/godzilla-minus-zero/ET00524518",
        "dates": ["2026-11-20", "2026-11-23", "2026-11-24"],
        "theaters": [PRIYA, ELEGANTE, PACIFIC_NSP],
    },
    {
        "name": "The Hunger Games: Sunrise on the Reaping",
        "url": "https://in.bookmyshow.com/movies/delhi/the-hunger-games-sunrise-on-the-reaping/ET00496180",
        "dates": ["2026-11-20", "2026-11-23", "2026-11-24"],
        "theaters": [PRIYA, ELEGANTE, PACIFIC_NSP],
    },
    {
        "name": "You Can See Everything",
        "url": "https://in.bookmyshow.com/movies/delhi/you-can-see-everything/ET00526232",
        "dates": ["2026-11-27", "2026-12-01", "2026-12-02"],
        "theaters": [PRIYA, ELEGANTE, PACIFIC_NSP],
    },
    {
        "name": "Dune: Part Three",
        "url": "https://in.bookmyshow.com/movies/delhi/dune-part-three/ET00491771",
        "dates": _days("2026-12-17", "2026-12-22"),
        "theaters": [PRIYA, ELEGANTE_PXL],
    },
    {
        "name": "Avengers: Doomsday",
        "url": "https://in.bookmyshow.com/movies/delhi/avengers-doomsday/ET00439706",
        "dates": _days("2026-12-17", "2026-12-22"),
        "theaters": [PRIYA, ELEGANTE_PXL],
    },
]


# ---------------------------------------------------------------------------
# config / helpers
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}] {msg}", flush=True)


def _force_utf8_console() -> None:
    # Windows consoles default to cp1252; emoji in subjects/venues would crash print()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_console()


def load_dotenv() -> None:
    """Tiny .env loader (no dependency): KEY=value lines, real env wins."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            os.environ.setdefault(k, v)


def _env(name: str, *, required: bool = False) -> str:
    val = os.getenv(name, "").strip()
    if not val and required:
        raise ConfigError(f"missing required setting: {name}")
    return val


def load_email_config(*, required: bool) -> dict:
    cfg = {
        "resend_api_key": _env("RESEND_API_KEY", required=required),
        "email_from": _env("EMAIL_FROM", required=required),
        "recipients": [r for r in (x.strip() for x in re.split(r"[;,]", _env("EMAIL_TO", required=required))) if r],
        "headless": os.getenv("HEADLESS", "false").strip().lower() == "true",
    }
    if required and not cfg["recipients"]:
        raise ConfigError("EMAIL_TO must contain at least one email address")
    return cfg


def now_iso() -> str:
    return datetime.now(IST).replace(microsecond=0).isoformat()


def monitor_key(url: str, datecode: str) -> str:
    return hashlib.sha256(f"v2|{url.lower()}|{CITY}|{datecode}".encode()).hexdigest()[:16]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def evaluate(result: dict, theaters: list) -> tuple[dict, dict]:
    """Return (matched, format_miss).

    matched:     venue -> rule label, for venues satisfying a rule fully.
    format_miss: venue -> (rule label, screen labels seen), for venues whose
                 name matches a format-restricted rule but no show is in that
                 format (yet).
    """
    attrs = result.get("venue_attrs") or {}
    formats = result.get("venue_formats") or {}
    shows = result.get("show_counts") or {}
    # BookMyShow silently answers with ANOTHER date's showtimes when the
    # requested date isn't on sale yet, so only venues with shows actually
    # dated on the target date count. (If no show carries a date code at all,
    # fall back to trusting the venue list.)
    dated = any(k for k in (result.get("venue_dates") or {}))
    venues = [v for v in (result.get("target_venues") or []) if not dated or shows.get(v, 0) > 0]
    matched: dict = {}
    format_miss: dict = {}
    for v in venues:
        for label, needle, fmt in theaters:
            if needle not in (v or "").lower():
                continue
            labels = list(attrs.get(v, [])) + list(formats.get(v, []))
            if fmt is None or any(_norm(fmt) in _norm(a) for a in labels):
                matched[v] = label
                format_miss.pop(v, None)
                break
            format_miss[v] = (label, labels)
    return matched, format_miss


# ---------------------------------------------------------------------------
# emails
# ---------------------------------------------------------------------------

def _wrap(inner: str) -> str:
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'color:#111;line-height:1.55;max-width:600px;margin:0 auto;padding:16px">'
        + inner
        + '<p style="color:#9ca3af;font-size:12px;margin-top:26px">Sent by your movie-notifier. '
        "Each venue is alerted once per movie/date; clear state.json to reset.</p></div>"
    )


def alert_email(movie: str, date_display: str, buy_url: str, rows: list, *, heads_up: bool) -> tuple[str, str]:
    """rows: [(venue, rule label, show count, screen labels)]"""
    if heads_up:
        headline = "👀 Theater open — but not in the format you want (yet)"
        detail = ("Your theater has shows listed, but none matched the required format. "
                  "Screen labels BookMyShow shows are listed below — check if one of them is actually the format you want. "
                  "You'll get another email when a matching show appears.")
    else:
        headline = "🎬 Tickets OPEN"
        detail = "BookMyShow has opened ticket sales at your theater — book before shows sell out!"
    lis = []
    for venue, label, cnt, labels in rows:
        extra = f" — {cnt} show{'' if cnt == 1 else 's'}" if cnt else ""
        if labels:
            extra += f" <span style=\"color:#6b7280\">[{html.escape(', '.join(labels))}]</span>"
        lis.append(f"<li><b>{html.escape(label)}</b>: {html.escape(venue)}{extra}</li>")
    inner = f"""
  <p style="margin:0;color:#6b7280;font-size:13px">{html.escape(movie)} · Delhi · {html.escape(date_display)}</p>
  <h1 style="font-size:21px;margin:6px 0 10px">{html.escape(headline)}</h1>
  <p style="margin:0 0 14px">{detail}</p>
  <a href="{html.escape(buy_url, quote=True)}" style="display:inline-block;background:#d90429;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;font-size:14px">Open BookMyShow &#8594;</a>
  <ul>{''.join(lis)}</ul>"""
    subject = f"{'👀 Heads-up' if heads_up else '🎬 Tickets OPEN'} · {movie} · {date_display} · " + ", ".join(
        sorted({r[1] for r in rows})
    )
    return subject, _wrap(inner)


def error_email(errors: list[tuple[str, str]]) -> tuple[str, str]:
    items = "".join(
        f"<li><b>{html.escape(what)}</b><pre style=\"white-space:pre-wrap;background:#f3f4f6;padding:8px;"
        f"border-radius:6px;font-size:12px\">{html.escape(msg[:600])}</pre></li>"
        for what, msg in errors
    )
    inner = f"""
  <h1 style="font-size:19px;margin:0 0 10px">⚠️ movie-notifier hit errors</h1>
  <p>These are new errors (repeats are only logged in the Actions run output). BookMyShow may just be flaky; it often recovers.</p>
  <ul>{items}</ul>"""
    return f"⚠️ movie-notifier: {len(errors)} check(s) failed", _wrap(inner)


def _send(cfg: dict, subject: str, body_html: str) -> None:
    log(f"sending email: {subject}")
    msg_id = send_email(
        api_key=cfg["resend_api_key"], from_=cfg["email_from"], to=cfg["recipients"],
        subject=subject, html=body_html,
    )
    log(f"email accepted by Resend (id={msg_id})")


# ---------------------------------------------------------------------------
# run logic
# ---------------------------------------------------------------------------

def _monitor_with_retry(mcfg: dict, args, cfg: dict, session: BrowserSession) -> dict:
    dump_dir = ROOT / "debug" if (args.dump or os.getenv("DUMP", "").lower() == "true") else None
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return run_monitor(mcfg, headless=cfg["headless"], dump_dir=dump_dir, log=log, session=session)
        except CloudflareBlocked:
            raise  # retrying from the same runner is pointless
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"attempt {attempt} failed: {type(exc).__name__}: {exc}")
            if attempt == 1:
                time.sleep(10)
    raise last  # type: ignore[misc]


def check_one(
    target: dict, iso_date: str, state: dict, cfg: dict, args, session: BrowserSession
) -> tuple[bool, tuple | None]:
    """Check one (movie, date). Returns (state changed, new error or None)."""
    d = date.fromisoformat(iso_date)
    datecode, date_display = d.strftime("%Y%m%d"), d.strftime("%a, %d %b %Y")
    key = monitor_key(target["url"], datecode)
    rec = dict(state["monitors"].get(key, {}))
    what = f"{target['name']} · {date_display}"
    log(f"--- {what}")

    mcfg = {"movie": target["url"], "city": CITY, "datecode": datecode, "date_display": date_display,
            "theater": "", "movie_is_url": True}
    try:
        result = _monitor_with_retry(mcfg, args, cfg, session)
    except CloudflareBlocked:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        sig = hashlib.sha256(msg.encode()).hexdigest()[:12]
        if args.dry_run:
            log(f"dry-run: error {msg}")
            return False, None
        if rec.get("last_error_sig") == sig:
            log(f"repeat of known error (not emailing again): {msg}")
            return False, None
        rec.update(last_error_sig=sig, last_error=msg[:600], error_at=now_iso())
        state["monitors"][key] = rec
        return True, (what, msg)

    shows = result.get("show_counts") or {}
    dated = any(k for k in (result.get("venue_dates") or {}))
    venues = [v for v in (result.get("target_venues") or []) if not dated or shows.get(v, 0) > 0]
    matched, format_miss = evaluate(result, target["theaters"])
    attrs = result.get("venue_attrs") or {}
    log(f"source={result.get('source')} venues={len(venues)} matched={list(matched) or '-'}"
        f" format_miss={list(format_miss) or '-'}")

    status = "open" if matched else ("format_pending" if format_miss else ("open_elsewhere" if venues else "watching"))
    changed = False
    for k, v in {"movie": target["name"], "url": target["url"], "date": datecode, "status": status}.items():
        if rec.get(k) != v:
            rec[k] = v
            changed = True
    if rec.pop("last_error_sig", None):
        rec.pop("last_error", None)
        rec.pop("error_at", None)
        changed = True

    notified = set(rec.get("notified_venues", []))
    headsup = set(rec.get("headsup_venues", []))
    new_open = [v for v in matched if args.force or v not in notified]
    new_headsup = [v for v in format_miss if args.force or (v not in headsup and v not in notified)]

    if args.dry_run:
        log(f"DRY-RUN: would alert {new_open or '-'}; would heads-up {new_headsup or '-'}")
        return False, None

    buy_url = result.get("buy_url", "")
    movie_disp = result.get("movie_display") or target["name"]
    if new_open:
        rows = [(v, matched[v], shows.get(v, 0), attrs.get(v, [])) for v in new_open]
        try:
            _send(cfg, *alert_email(movie_disp, date_display, buy_url, rows, heads_up=False))
            rec["notified_venues"] = sorted(notified | set(new_open))
            rec["notified_at"] = now_iso()
            changed = True
        except Exception as exc:  # noqa: BLE001
            return changed, (what, f"alert email failed: {type(exc).__name__}: {exc}")
    if new_headsup:
        rows = [(v, format_miss[v][0], shows.get(v, 0), format_miss[v][1]) for v in new_headsup]
        try:
            _send(cfg, *alert_email(movie_disp, date_display, buy_url, rows, heads_up=True))
            rec["headsup_venues"] = sorted(headsup | set(new_headsup))
            changed = True
        except Exception as exc:  # noqa: BLE001
            return changed, (what, f"heads-up email failed: {type(exc).__name__}: {exc}")

    state["monitors"][key] = rec
    return changed, None


def run_flow(args) -> int:
    load_dotenv()
    try:
        cfg = load_email_config(required=not args.dry_run)
    except ConfigError as exc:
        log(f"CONFIG ERROR: {exc}")
        return 2

    state = load_state(STATE_FILE)
    today = datetime.now(IST).date().isoformat()
    changed = False
    errors: list[tuple[str, str]] = []
    blocked = False
    # One shared browser for the whole run; only started if some check
    # actually needs the browser fallback.
    with BrowserSession(headless=cfg["headless"]) as session:
        try:
            for target in TARGETS:
                for iso_date in target["dates"]:
                    if iso_date < today:
                        continue
                    c, err = check_one(target, iso_date, state, cfg, args, session)
                    changed |= c
                    if err:
                        errors.append(err)
        except CloudflareBlocked as exc:
            # GitHub runner IPs are sometimes on Cloudflare's blocklist; every
            # remaining check would fail identically. Stop now — the next run
            # lands on a different runner/IP and normally works.
            blocked = True
            log(f"Cloudflare is blocking this runner ({exc}) — skipping the remaining checks this run")
            print("::warning::Cloudflare blocked this runner's IP; checks skipped this run (next run will retry)",
                  flush=True)

    if args.dry_run:
        return 0
    if errors:
        try:
            _send(cfg, *error_email(errors))
        except Exception as exc:  # noqa: BLE001
            log(f"error email also failed: {type(exc).__name__}: {exc}")

    if changed or heartbeat_due(state):
        if heartbeat_due(state):
            state["last_heartbeat_at"] = now_iso()
        state["last_commit_at"] = now_iso()
        save_state(STATE_FILE, state)
        if git_commit_file(STATE_FILE, "state: update [skip ci]"):
            log("state.json committed to the repository")
        else:
            log("state.json saved locally (no git repo or commit failed)")
    else:
        log("state unchanged — nothing to save/commit")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BookMyShow ticket-open watcher")
    parser.add_argument("--dry-run", action="store_true", help="detect only; no email, no state writes")
    parser.add_argument("--force", action="store_true", help="re-send alerts even if already notified")
    parser.add_argument("--dump", action="store_true", help="save intercepted API payloads to debug/")
    args = parser.parse_args()
    return run_flow(args)


if __name__ == "__main__":
    sys.exit(main())
