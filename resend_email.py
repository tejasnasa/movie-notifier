"""Send email through Resend's HTTP API (https://api.resend.com/emails).

Uses only the standard library — no requests dependency needed. The "from"
address must be on a domain verified in your Resend dashboard.

Note: api.resend.com sits behind Cloudflare. A bare `Python-urllib` UA gets a
403 "error code: 1010" (bot-signature ban), so we always send a real UA and
fall back to a browser UA once if Cloudflare still objects.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

RESEND_URL = "https://api.resend.com/emails"

UA_APP = "movie-notifier/1.0 (GitHub Actions checker)"
UA_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


class EmailSendError(Exception):
    pass


def _post(api_key: str, data: bytes, ua: str, timeout: float):
    req = urllib.request.Request(
        RESEND_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": ua,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise EmailSendError(f"Resend HTTP {exc.code}: {exc.reason} {detail}".strip()) from exc
    except urllib.error.URLError as exc:
        raise EmailSendError(f"network error contacting Resend: {exc.reason}") from exc


def send_email(
    *,
    api_key: str,
    from_: str,
    to: list[str],
    subject: str,
    html: str,
    timeout: float = 30.0,
) -> str:
    """POST the message to Resend; returns the message id on success."""
    if not api_key:
        raise EmailSendError("RESEND_API_KEY is empty")
    if not to:
        raise EmailSendError("EMAIL_TO has no addresses")
    payload = {
        "from": from_,
        "to": list(to),
        "subject": subject,
        "html": html,
    }
    data = json.dumps(payload).encode("utf-8")
    try:
        body = _post(api_key, data, UA_APP, timeout)
    except EmailSendError as first:
        # Cloudflare signature ban retry with a browser-like UA
        msg = str(first)
        if "HTTP 403" in msg and ("1010" in msg or "Cloudflare" in msg):
            body = _post(api_key, data, UA_BROWSER, timeout)
        else:
            raise
    try:
        return str(json.loads(body).get("id") or "")
    except Exception:
        return ""
