#!/usr/bin/env python3
"""movie-notifier — watch BookMyShow for ticket availability and email an alert.

Config comes from environment variables (GitHub secrets in Actions, or a .env
file locally). State is kept in state.json and committed back to the repo on
GitHub Actions so the run is never repeated / not forgotten.

Usage:
  python main.py             # normal run
  python main.py --dry-run   # detect only; never send email or change state
  python main.py --force     # send the alert email even if already notified
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bms import MonitorError, run_monitor
from resend_email import send_email
from state_store import git_commit_file, heartbeat_due, load_state, save_state

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
IST = timezone(timedelta(hours=5, minutes=30))
URL_RE = re.compile(r"^https?://", re.I)


# ---------------------------------------------------------------------------
# config
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
    # utf-8-sig strips the BOM PowerShell's Set-Content -Encoding UTF8 writes
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            os.environ.setdefault(k, v)


def _env(name: str, *, required: bool = False, default: str = "") -> str:
    val = os.getenv(name, "").strip()
    if val:
        return val
    if required:
        raise ConfigError(f"missing required setting: {name}")
    return default


def parse_monitor_date(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            d = datetime.strptime(raw, fmt).date()
            return d.strftime("%Y%m%d"), d.strftime("%a, %d %b %Y")
        except ValueError:
            continue
    raise ConfigError(f"MONITOR_DATE must be YYYY-MM-DD or YYYYMMDD, got {raw!r}")


def load_config(*, require_email: bool = True) -> dict:
    movie = _env("MOVIE", required=True)
    city = _env("CITY", required=True).lower().replace(" ", "-")
    datecode, date_display = parse_monitor_date(_env("MONITOR_DATE", required=True))
    req = require_email
    cfg = {
        "movie": movie,
        "city": city,
        "datecode": datecode,
        "date_display": date_display,
        "theater": _env("THEATER", default=""),
        "resend_api_key": _env("RESEND_API_KEY", required=req),
        "email_from": _env("EMAIL_FROM", required=req),
        "recipients": [r for r in (x.strip() for x in re.split(r"[;,]", _env("EMAIL_TO", required=req))) if r],
        "headless": os.getenv("HEADLESS", "false").strip().lower() == "true",
    }
    if req and not cfg["recipients"]:
        raise ConfigError("EMAIL_TO must contain at least one email address")
    cfg["movie_is_url"] = bool(URL_RE.match(cfg["movie"]))
    return cfg


def monitor_key(cfg: dict) -> str:
    blob = "|".join(
        [
            cfg["movie"].strip().lower(),
            cfg["city"],
            cfg["datecode"],
            cfg["theater"].strip().lower(),
        ]
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(IST).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# emails
# ---------------------------------------------------------------------------

def _venues_html(names: list, shows: dict, limit: int = 15) -> str:
    lis = []
    for n in names[:limit]:
        cnt = shows.get(n) or 0
        suffix = f" — {cnt} show{'' if cnt == 1 else 's'}" if cnt else ""
        lis.append(f"<li>{html.escape((n or '')[:120])}{suffix}</li>")
    if len(names) > limit:
        lis.append(f"<li>… and {len(names) - limit} more</li>")
    return "<ul>" + "".join(lis) + "</ul>" if lis else "<p>(no venue list available)</p>"


def theater_match(names: list, theater: str) -> list:
    if not theater:
        return list(names)
    t = theater.strip().lower()
    return [n for n in names if t in (n or "").lower()]


def _email_shell(headline: str, movie_display: str, cfg: dict, detail: str) -> tuple[str, str]:
    """Return (subject, html)."""
    city_disp = cfg["city"].replace("-", " ").title()
    subject = f"{headline} · {movie_display} · {city_disp} · {cfg['date_display']}"
    body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111;line-height:1.55;max-width:600px;margin:0 auto;padding:16px">
  <p style="margin:0;color:#6b7280;font-size:13px">{html.escape(movie_display)} · {html.escape(city_disp)} · {html.escape(cfg['date_display'])}</p>
  <h1 style="font-size:21px;margin:6px 0 10px">{html.escape(headline)}</h1>
  <p style="margin:0 0 14px">{detail}</p>
  <a href="{html.escape(cfg['buy_url'], quote=True)}" style="display:inline-block;background:#d90429;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;font-size:14px">Open BookMyShow &#8594;</a>
  <h3 style="margin:20px 0 6px;font-size:14px">{html.escape(cfg['list_caption'])}</h3>
  {_venues_html(cfg['list_names'], cfg['shows'])}
  <p style="color:#9ca3af;font-size:12px;margin-top:26px">Sent by your movie-notifier (hourly check). Alert emails are sent once per milestone; change or clear state.json to reset notifications.</p>
</div>"""
    return subject, body


def alert_email_parts(cfg: dict, result: dict, stage: int) -> tuple[str, str]:
    """stage 1 = open at some venue but not the requested theater; stage 2 = final alert."""
    movie_display = result.get("movie_display") or cfg["movie"]
    cfg = dict(cfg)
    matched = theater_match(result["target_venues"], cfg["theater"])
    names = matched if matched else result["target_venues"]
    cfg["shows"] = result.get("show_counts", {})
    cfg["buy_url"] = result.get("buy_url", "")
    if stage == 2:
        cfg["list_names"] = names
        cfg["list_caption"] = "Venues"
        headline = "🎬 Tickets OPEN"
        detail = "BookMyShow has opened ticket sales."
        if cfg["theater"]:
            detail += f" Your filter \u201c{cfg['theater']}\u201d matched — book before shows sell out!"
    else:
        cfg["list_names"] = result["target_venues"]
        cfg["list_caption"] = "All venues (your filter hasn't matched yet)"
        headline = "🎬 Tickets just opened for others…"
        detail = (
            f"BookMyShow started selling tickets, but no venue matched \u201c{cfg['theater']}\u201d yet. "
            "You will get one more email as soon as it does."
        )
    return _email_shell(headline, movie_display, cfg, detail)


def error_email_parts(cfg: dict, message: str) -> tuple[str, str]:
    movie_display = cfg["movie"]
    cfg = dict(cfg)
    cfg["shows"] = {}
    cfg["buy_url"] = ""
    cfg["list_names"] = []
    cfg["list_caption"] = ""
    body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111;line-height:1.55;max-width:600px;margin:0 auto;padding:16px">
  <h1 style="font-size:19px;margin:0 0 10px">⚠️ movie-notifier failed</h1>
  <p style="margin:0 0 6px;color:#6b7280;font-size:13px">{html.escape(movie_display)} · {html.escape(cfg['city'])} · {html.escape(cfg['date_display'])}</p>
  <p style="margin:10px 0">The hourly check hit an error. This is the first occurrence of this error; later identical errors are only logged in the Actions run output.</p>
  <pre style="white-space:pre-wrap;background:#f3f4f6;padding:10px;border-radius:6px;font-size:12px">{html.escape(message[:800])}</pre>
  <p style="color:#9ca3af;font-size:12px;margin-top:24px">If BookMyShow changed their page or a temporary outage happened, it may recover on its own. Check the Actions log for the latest status.</p>
</div>"""
    subject = f"⚠️ movie-notifier error — {movie_display} ({cfg['city']})"
    return subject, body


def _try_send_email(cfg: dict, subject: str, body_html: str) -> str:
    log(f"sending email: {subject}")
    msg_id = send_email(
        api_key=cfg["resend_api_key"],
        from_=cfg["email_from"],
        to=cfg["recipients"],
        subject=subject,
        html=body_html,
    )
    log(f"email accepted by Resend (id={msg_id})")
    return str(msg_id)


# ---------------------------------------------------------------------------
# run logic
# ---------------------------------------------------------------------------

def _error_path(cfg, state, key, rec, message, *, send_error_email=True) -> int:
    sig = hashlib.sha256(message.encode()).hexdigest()[:12]
    changed = False
    if rec.get("last_error_sig") != sig:
        rec = dict(rec)
        rec.update(
            last_error_sig=sig,
            last_error=message[:600],
            error_at=now_iso(),
        )
        state["monitors"][key] = rec
        changed = True
        log(f"error recorded: {message}")
        if send_error_email:
            subject, body = error_email_parts(cfg, message)
            try:
                _try_send_email(cfg, subject, body)
            except Exception as exc:
                log(f"error email also failed: {type(exc).__name__}: {exc}")
    else:
        log(f"repeat of known error (not emailing again): {message}")
    _maybe_commit(state, changed, f"state: error recorded [skip ci]")
    return 1


def _maybe_commit(state: dict, changed: bool, message: str) -> None:
    if not changed and not heartbeat_due(state):
        log("state unchanged — nothing to save/commit")
        return
    if heartbeat_due(state):
        state["last_heartbeat_at"] = now_iso()
        log("heartbeat due — writing state to keep repository activity alive")
    state["last_commit_at"] = now_iso()
    message = message or "state: heartbeat [skip ci]"
    save_state(STATE_FILE, state)
    if git_commit_file(STATE_FILE, message):
        log("state.json committed to the repository")
    else:
        log("state.json saved locally (no git repo or commit failed)")


def run_flow(args) -> int:
    load_dotenv()
    try:
        cfg = load_config(require_email=not args.dry_run)
    except ConfigError as exc:
        log(f"CONFIG ERROR: {exc}")
        return 2
    log(
        "monitoring: " + json.dumps(
            {
                "movie": cfg["movie"],
                "city": cfg["city"],
                "date": cfg["date_display"],
                "theater": cfg["theater"] or "(any)",
                "recipients": len(cfg["recipients"]),
                "headless": cfg["headless"],
            }
        )
    )

    state = load_state(STATE_FILE)
    key = monitor_key(cfg)
    rec = dict(state["monitors"].get(key, {}))

    # -- monitor --------------------------------------------------------------
    result = None
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            dump_dir = ROOT / "debug" if (args.dump or os.getenv("DUMP", "").lower() == "true") else None
            result = run_monitor(cfg, headless=cfg["headless"], dump_dir=dump_dir, log=log)
            break
        except Exception as exc:  # noqa: BLE001 - we want full isolation here
            last_err = exc
            log(f"attempt {attempt} failed: {type(exc).__name__}: {exc}")
            if attempt == 1:
                time.sleep(20)
    if result is None:
        log("monitor failed after retries")
        if args.dry_run:
            log("dry-run: no state changes, no emails")
            return 1
        return _error_path(cfg, state, key, rec, f"{type(last_err).__name__}: {last_err}")

    # -- decision -------------------------------------------------------------
    venues = result.get("target_venues", []) or []
    matched = theater_match(venues, cfg["theater"])
    log(
        f"result: source={result.get('source')} date_state={result.get('date_state')} "
        f"venues={len(venues)} matching_theater={len(matched)}"
    )
    if venues:
        for n in venues[:8]:
            log(f"  venue: {n}")
        if len(venues) > 8:
            log(f"  … and {len(venues) - 8} more")
    if not venues:
        log("tickets not opened yet — no venues for the monitored date")

    stage_target = 2 if matched else (1 if venues else 0)
    status_new = {0: "watching", 1: "open_elsewhere", 2: "open"}[stage_target]

    if args.dry_run:
        log(
            f"DRY-RUN: status would be '{status_new}'; "
            f"would email: {'final alert' if stage_target == 2 else 'heads-up alert' if stage_target == 1 else 'nothing'}; "
            f"state untouched, no emails sent"
        )
        return 0

    # -- update state / send mail ---------------------------------------------
    changed = False
    prev_status = rec.get("status", "")
    if prev_status != status_new:
        rec["status"] = status_new
        changed = True
    meta = {
        "movie": cfg["movie"],
        "movie_display": result.get("movie_display") or cfg["movie"],
        "city": cfg["city"],
        "date": cfg["datecode"],
        "theater": cfg["theater"],
    }
    for k, v in meta.items():
        if rec.get(k) != v:
            rec[k] = v
            changed = True

    notified_stage = int(rec.get("notified_stage", 0) or 0)
    email_stage = 0
    if stage_target >= 1:
        if args.force:
            email_stage = stage_target
        elif stage_target == 2 and notified_stage < 2:
            email_stage = 2
        elif stage_target == 1 and notified_stage < 1:
            email_stage = 1

    if email_stage:
        subject, body = alert_email_parts(cfg, result, email_stage)
        try:
            _try_send_email(cfg, subject, body)
        except Exception as exc:
            log(f"alert email failed: {type(exc).__name__}: {exc}")
            state["monitors"][key] = rec
            return _error_path(cfg, state, key, rec, f"alert email failed: {type(exc).__name__}: {exc}")
        if email_stage == 2:
            rec["notified_final_at"] = now_iso()
        rec["notified_stage"] = max(notified_stage, email_stage)
        rec["notified_at"] = rec.get("notified_final_at") or now_iso()
        changed = True

    state["monitors"][key] = rec
    if changed:
        _maybe_commit(state, True, f"state: {status_new} [skip ci]")
    else:
        _maybe_commit(state, False, "")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BookMyShow ticket-open watcher")
    parser.add_argument("--dry-run", action="store_true", help="detect only; no email, no state writes")
    parser.add_argument("--force", action="store_true", help="send the alert email even if already notified")
    parser.add_argument("--dump", action="store_true", help="save intercepted API payloads to debug/")
    args = parser.parse_args()
    return run_flow(args)


if __name__ == "__main__":
    sys.exit(main())
