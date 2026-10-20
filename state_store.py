"""state.json persistence + the git commit/push heartbeat.

state.json layout (committed back to the repo from Actions):

{
  "version": 1,
  "last_commit_at": "<iso>",     // set whenever we write+commit (heartbeat clock)
  "last_heartbeat_at": "<iso>",  // set when a no-op heartbeat write happened
  "monitors": {
    "<config-hash>": {
      "movie": "...", "city": "...", "date": "...", "theater": "...",
      "status": "watching" | "open_elsewhere" | "open",
      "notified_stage": 0|1|2,
      "notified_at": "<iso>",
      "notified_final_at": "<iso>",
      "last_error_sig": "...", "last_error": "...", "error_at": "<iso>"
    }
  }
}

The heartbeat keeps GitHub's 60-day-scheduled-workflow auto-disable away:
a commit lands at most once every HEARTBEAT_HOURS even if nothing changes.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

HEARTBEAT_HOURS = 20

STATE_VERSION = 1


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("monitors"), dict):
            data.setdefault("version", STATE_VERSION)
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {"version": STATE_VERSION, "monitors": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["last_commit_at"] = state.get("last_commit_at") or ""
    # atomic-ish write so a crashed run can't leave a truncated state file
    tmp_name: str | None = None
    fd = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False)
    try:
        json.dump(state, fd, indent=2, ensure_ascii=False)
        fd.flush()
        tmp_name = fd.name
    finally:
        fd.close()
    if tmp_name:
        Path(tmp_name).replace(path)


def heartbeat_due(state: dict) -> bool:
    raw = str(state.get("last_commit_at") or "").strip()
    if not raw:
        return False  # fresh state will be committed anyway because content changes
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return False
    elapsed_h = (datetime.now(last.tzinfo) - last).total_seconds() / 3600.0
    return elapsed_h >= HEARTBEAT_HOURS


def git_commit_file(path: Path, message: str) -> bool:
    """Commit + push a single file if anything actually changed. Best effort.

    Returns True when a commit+push happened, False when there was nothing to
    commit, no git repo, or pushing isn't available (e.g. local run).
    """
    path = Path(path).resolve()
    repo = path.parent
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return False

        subprocess.run(["git", "add", path.name], cwd=str(repo), capture_output=True, text=True, timeout=30)
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", path.name],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not status.stdout.strip():
            return False  # identical to HEAD; nothing to commit

        for k, v in (
            ("user.name", "movie-notifier[bot]"),
            ("user.email", "movie-notifier[bot]@users.noreply.github.com"),
        ):
            subprocess.run(["git", "config", k, v], cwd=str(repo), capture_output=True, text=True, timeout=30)

        commit = subprocess.run(
            ["git", "commit", "-m", message], cwd=str(repo), capture_output=True, text=True, timeout=60
        )
        if commit.returncode != 0:
            return False

        push = subprocess.run(["git", "push"], cwd=str(repo), capture_output=True, text=True, timeout=120)
        if push.returncode != 0:
            # one silent retry after a rebase (rare push race)
            subprocess.run(
                ["git", "pull", "--rebase", "--autostash"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=120,
            )
            push = subprocess.run(["git", "push"], cwd=str(repo), capture_output=True, text=True, timeout=120)
            if push.returncode != 0:
                return False
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
