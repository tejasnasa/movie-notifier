# movie-notifier

Get an email the moment BookMyShow opens ticket sales for a specific movie /
date / city (optionally only when a specific theater appears) — checked every
hour by a free GitHub Actions job, delivered by [Resend](https://resend.com).

```
┌──────────────────── GitHub Actions (hourly cron) ────────────────────┐
│  movie-notifier                                                      │
│    1. resolve movie (name / ET code / URL) → ET code + slug           │
│    2. FAST PATH: call BookMyShow's showtimes API directly with a      │
│       faked browser-fingerprint header set (no browser, ~2-5s)        │
│    3. fallback: open the buy-tickets page in a headed browser (Xvfb)  │
│       and read the SSR-embedded showtimes state                       │
│    4. venues found? → "Tickets OPEN" email via Resend                 │
│    5. remember last state in state.json (committed back to the repo)  │
└───────────────────────────────────────────────────────────────────────┘
```

## How it works

- **The fast path (default, no browser at all):** the watcher calls
  `https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic`
  — the same endpoint BookMyShow's own page uses — from a plain HTTPS client.
  It passes the site's bot filter because it fakes the *entire* scored
  fingerprint: Chrome User-Agent + `sec-ch-ua` client hints + `Referer` +
  BookMyShow's own `x-app-code` / `x-region-code` / geo headers. Regions come
  from BookMyShow's public `regions.json` (bmscdn). A typical check finishes
  in ~2–5 seconds.
  - Only `MOVIE` given as a **URL or ET code** uses the fast path; a movie
    *name* needs the explore pages, which plain clients can't fetch (the bot
    filter demands a real browser there) → browser path.
  - **Format variants are watched together.** BookMyShow tickets IMAX 2D /
    4DX / ICE / other-language cuts under their *own* event codes, and an
    IMAX-only cinema won't appear in the base code's venue list. The fast
    path reads the movie's format-selector from the payload and unions the
    venue lists of every variant (a few extra tiny API calls), so
    `THEATER=Priya` matches "PVR IMAX with Laser, Priya: Delhi" even though
    it only sells the IMAX-2D code, not the base one.
  - If the city isn't canonical in `regions.json`, or Cloudflare starts
    challenging, or the payload shape changes — the run **automatically falls
    back** to the headed browser. The fast path never answers with data it
    isn't sure about (e.g. a guessed region), so it can't produce false
    "open"/"not open" results.
- **The browser fallback:** the buy-tickets pages
  (`/movies/{city}/{slug}/buytickets/{etcode}/{yyyymmdd}`)
  server-render the entire showtimes payload into
  `window.__INITIAL_STATE__.showtimesFunctionalApi` (an RTK Query cache). The
  bot walks that state in the browser.
- BookMyShow's Cloudflare hard-blocks headless Chromium (403), so the browser
  fallback runs a **headed** (visible-looking) browser inside a virtual
  framebuffer (Xvfb) on the GitHub runner — no display required.
- Trigger semantics (you get at most 2 alert emails):
  - `status=watching` — website checked, no venues listed for your date.
  - `status=open_elsewhere` — tickets opened, but your theater filter hasn't
    matched yet → heads-up email sent **once**.
  - `status=open` — your filter matches (or no filter set) → final email.
  - Broken site / failed checks → one failure email per *new* error signature,
    never hourly spam.
- `state.json` in the repo root remembers status per monitor config
  (movie+city+date+theater hash), so alerts fire exactly-once. A no-op
  heartbeat commit happens at most once every 20h, which keeps GitHub from
  auto-disabling the scheduled workflow (60-day inactivity rule).

## Setup

### 1. Resend

1. In the [Resend dashboard](https://dashboard.resend.com): add & verify the
   domain you'll send from.
2. Create an **API key**.
3. Note a sender like `Movie Notifier <alerts@your-domain.in>`.

### 2. GitHub repo

1. Create a **public** repo (public = unlimited free Actions minutes).
2. Push all files in this folder (including `.github/workflows/notify.yml` and
   `state.json` placeholder if present).
3. GitHub will auto-enable Actions on the default branch.

### 3. Variables (and the one secret)

Repo → *Settings* → *Secrets and variables* → *Actions*:

- **Secrets tab** → *New repository secret* — only one:

| Secret           | Value                     | Required |
|------------------|---------------------------|----------|
| `RESEND_API_KEY` | Resend API key (`re_...`) | yes      |

- **Variables tab** → *New repository variable* — everything else (these are
  visible and editable without touching secrets):

| Variable       | Value                                              | Required |
|----------------|----------------------------------------------------|----------|
| `EMAIL_FROM`   | `Movie Notifier <alerts@your-domain.in>`           | yes      |
| `EMAIL_TO`     | comma-separated recipients                         | yes      |
| `MOVIE`        | see "Choosing MOVIE" below                         | yes      |
| `CITY`         | e.g. `bengaluru` (BookMyShow city slug)            | yes      |
| `MONITOR_DATE` | `2026-10-19` or `20261019`                         | yes      |
| `THEATER`      | theater name substring (e.g. `PVR: Orion`)         | no       |

The workflow falls back to the old secret names (`vars.X || secrets.X`), so
if you had already created these as secrets, they keep working — but new
setups should use variables (they're easier to update later).

### Choosing MOVIE (most → least reliable)

1. **Full BookMyShow movie-page URL** (best): open the movie on
   bookmyshow.com and copy the address, e.g.
   `https://in.bookmyshow.com/movies/bengaluru/jailer-2/ET00518674`.
   Works even for movies that aren't yet in any top-10 list.
2. **ET code**: `ET00518674` (shown in the movie page URL). Resolved via the
   site's explore pages.
3. **Movie name**: `Jailer 2`. Matched against BookMyShow's *now-showing* +
   *top upcoming* JSON-LD lists for your city — works while the movie is
   visible in those lists.

### First run / testing

- Actions tab → **movie-notifier** → *Run workflow*. Set **debug = true** to
  save the extracted state as a debug artifact (raw API payloads, page text).
- Default schedule: hourly at :23 UTC — change the `cron` in the workflow file
  to whatever cadence you like.
- Typical run: ~1 min total (Playwright install is cached; the check itself is
  seconds when the fast path answers). If logs show the browser fallback
  running (Xvfb + Chromium), add ~1–2 min — still well inside the GitHub
  free tier.

## Changing the alert semantics

- **Re-arm an alert** (email again next time it opens): delete the monitor's
  record in `state.json` (the object keyed by a 16-char hash) or the whole
  `monitors` map, and commit.
- **Force-send now**: run workflow_dispatch (or locally `python main.py --force`)
  — sends the current milestone alert again.
- **Change date/theater/movie**: change the repo *variables* (no secrets
  touched); the config hash changes, so a fresh monitor is created and alerts
  re-arm automatically.

## Local usage

```powershell
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env    # then edit
python main.py            # actual run (needs a screen; Cloudflare blocks headless)
python main.py --dry-run  # check detection without sending email/writing state
python main.py --dump     # also save debug/ artifacts
```

Notes:
- Local runs update `state.json` but can't commit (no git repo) — that's fine.
- `HEADLESS=false` (default) applies to the browser fallback only; the fast
  path never opens a browser. Set `BMS_FASTPATH=false` in `.env` to force the
  browser path (useful for debugging the fallback).
- The dummy/invalid test key produces `Resend HTTP 401` on purpose; use a real
  key when you want mail to actually go out.

## Files

| File                          | Purpose                                |
|-------------------------------|----------------------------------------|
| `main.py`                     | config, decision logic, emails, state  |
| `bms.py`                      | monitor orchestration + browser fallback (Playwright SSR extraction) |
| `bms_api.py`                  | fast path: direct showtimes API client (no browser) |
| `bms_common.py`               | shared resolution/venue-row helpers    |
| `resend_email.py`             | Resend HTTP API client (std-lib only)  |
| `state_store.py`              | state.json + git heartbeat commit      |
| `.github/workflows/notify.yml`| hourly cron job                        |
| `state.json`                  | alert memory (committed to repo)       |

## Debugging a failing run

1. Actions → run logs (the bot prints `fast path:` / `resolved via …`, venue
   list, decision). If the log starts with
   `fast path skipped (FastPathError: … — using the headed browser)`, the
   browser fallback handled the run — the reason tells you why (movie name
   input, city not in regions.json, Cloudflare challenge, payload change).
2. Run workflow_dispatch with `debug=true` → artifacts contain
   `debug/state_summary.json` (RTK cache rows), `primary_dynamic_v4.json`
   (fast-path payload) / `primary_dynamic_*.json` (SSR payload) and
   `page_text.txt`.
3. Common causes: movie not in top lists (→ use URL), Cloudflare challenge
   (transient; the workflow auto-retries), Resend domain not verified.
4. To test the browser fallback on purpose, set `BMS_FASTPATH=false`.

## Limitations & disclaimers

- The fast path calls BookMyShow's own showtimes API with a faked
  browser-fingerprint header set; the site's bot filter may tighten at any
  time (e.g. start requiring a real JS challenge or TLS fingerprinting), at
  which point the run silently switches to the headed-browser method — check
  the log line to see which source answered (`source=api` vs `source=state`).
- Scrapes BookMyShow's public pages; a redesign of `__INITIAL_STATE__` or the
  buy-tickets route might require a small parser fix. Generic fallbacks
  (network interception, DOM empty-state check) are in place.
- Runs at most one page-load per hour (zero when the fast path answers) —
  respectful of the site & their rate limits.
- Ticket data is checked as BookMyShow publishes it; actual availability at
  click-time may have changed.
