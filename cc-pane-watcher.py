#!/usr/bin/env python3
"""cc-pane-watcher — alert Discord when a CC tmux window is waiting for input.

Polls configured tmux windows every 5s. Detects the '❯' option-cursor that
Claude Code shows during permission prompts and AskUserQuestion dialogs.
Posts a ⏳ alert to the relevant Discord thread with the prompt context.
Clears (deletes alert message) once the cursor is gone.

Configuration: reads window→thread mapping from ~/.config/cc-bridge-threads/.
Each file in that directory is named after a tmux window and contains a
Discord thread/channel snowflake ID.

Alternatively, set THREAD_MAP directly in config.py if you prefer static config.

Token: reads DISCORD_BOT_TOKEN_CCODE from ~/.config/cc-bridge-token.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# tmux session name to watch
SESSION = os.environ.get("CC_BRIDGE_SESSION", "cc-main")

# Thread map: window name → Discord thread/channel ID
# Loaded from config.py if present, otherwise built from ~/.config/cc-bridge-threads/
THREAD_MAP: dict[str, str] = {}

try:
    import config as _cfg
    if hasattr(_cfg, "CHANNELS"):
        for _ch in _cfg.CHANNELS:
            THREAD_MAP[_ch["name"]] = _ch["thread_id"]
        SESSION = getattr(_cfg, "TMUX_SESSION", SESSION)
except ImportError:
    pass

# Fall back to reading ~/.config/cc-bridge-threads/ if not set via config.py
if not THREAD_MAP:
    _threads_dir = Path(os.path.expanduser("~/.config/cc-bridge-threads"))
    if _threads_dir.is_dir():
        for _f in _threads_dir.iterdir():
            if _f.is_file() and not _f.name.startswith("."):
                _tid = _f.read_text().strip()
                if _tid:
                    THREAD_MAP[_f.name] = _tid

TOKEN_FILE  = os.path.expanduser("~/.config/cc-bridge-token")
POLL_S      = 5
ALERT_LINES = 8   # lines of pane context to include in alert
MIN_REALERT_S = 90  # re-alert same window only if content changed or cooldown expired

API = "https://discord.com/api/v10"


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def _load_token() -> str:
    for line in Path(TOKEN_FILE).read_text().splitlines():
        m = re.match(r'\s*(?:export\s+)?DISCORD_BOT_TOKEN_CCODE\s*=\s*[\'"]?([^\s\'"]+)', line)
        if m:
            return m.group(1)
    raise RuntimeError("DISCORD_BOT_TOKEN_CCODE not found in ~/.config/cc-bridge-token")


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------

def _discord(method: str, path: str, tok: str, payload: dict | None = None) -> dict | None:
    url = f"{API}{path}"
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json",
                 "User-Agent": "cc-pane-watcher"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                retry = float(json.loads(e.read()).get("retry_after", 5))
            except Exception:
                retry = 5.0
            time.sleep(min(retry, 30))
        return None
    except Exception:
        return None


def post_alert(thread_id: str, text: str, tok: str) -> str | None:
    """Post alert message, return message_id or None."""
    r = _discord("POST", f"/channels/{thread_id}/messages",
                 tok, {"content": text, "allowed_mentions": {"parse": []}})
    return str(r["id"]) if r and "id" in r else None


def delete_message(thread_id: str, msg_id: str, tok: str) -> None:
    _discord("DELETE", f"/channels/{thread_id}/messages/{msg_id}", tok)


# ---------------------------------------------------------------------------
# Pane capture + detection
# ---------------------------------------------------------------------------

def capture_pane(window: str) -> str:
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", f"{SESSION}:{window}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def extract_prompt_context(pane_text: str) -> str:
    """Return the lines around the ❯ cursor, stripped of ANSI."""
    lines = [_strip_ansi(l).rstrip() for l in pane_text.splitlines()]

    # Find the ❯ line
    cursor_idx = next((i for i, l in enumerate(lines) if "❯" in l), None)
    if cursor_idx is None:
        return ""

    # Grab context: a few lines before + the option lines after
    start = max(0, cursor_idx - 5)
    end   = min(len(lines), cursor_idx + 4)
    snippet = "\n".join(lines[start:end]).strip()

    # Remove blank-only lines at edges
    return snippet


# ---------------------------------------------------------------------------
# ANSI stripping
# ---------------------------------------------------------------------------

_ANSI_CSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')   # CSI sequences (colors, cursor)
_ANSI_ESC = re.compile(r'\x1b[^\[]')                 # Other lone ESC sequences
_CTRL_CHR = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]') # Control chars (keep \t, \n)


def _strip_ansi(text: str) -> str:
    text = _ANSI_CSI.sub('', text)
    text = _ANSI_ESC.sub('', text)
    text = _CTRL_CHR.sub('', text)
    return text


# ❯ followed by an option: numbered ("❯ 1. Yes") or approval word ("❯ Yes", "❯ No")
# The plain input cursor "❯ " alone (nothing after) does NOT match.
_OPTION_CURSOR = re.compile(
    r'❯\s+(?:\d+\.|\b(?:Yes|No|Allow|Deny|Cancel|Skip|Proceed|Approve|Reject|Always)\b)',
    re.IGNORECASE,
)
# CC shows "⎿  Waiting…" when a tool call is pending user approval
_WAITING = re.compile(r'⎿\s+Waiting')


def is_waiting(pane_text: str) -> bool:
    """True if the pane shows a pending approval dialog or tool waiting for input."""
    clean = _strip_ansi(pane_text)
    return bool(_OPTION_CURSOR.search(clean) or _WAITING.search(clean))


# Regression guard: ANSI codes must not prevent prompt detection
assert is_waiting("\x1b[38;5;153m❯\x1b[39m \x1b[38;5;246m1. Yes"), "is_waiting ANSI regression"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    if not THREAD_MAP:
        print(
            "ERROR: No thread map configured. Either:\n"
            "  1. Set CHANNELS in config.py (copy config.example.py), or\n"
            "  2. Create files under ~/.config/cc-bridge-threads/<window_name> "
            "each containing a Discord thread ID.",
            file=sys.stderr,
        )
        sys.exit(1)

    tok = _load_token()

    # Per-window state: {"alert_msg_id": str|None, "last_context": str, "alerted_at": float}
    state: dict[str, dict] = {
        w: {"alert_msg_id": None, "last_context": "", "alerted_at": 0.0}
        for w in THREAD_MAP
    }

    print(f"cc-pane-watcher started — session={SESSION}, watching {list(THREAD_MAP.keys())}", flush=True)

    while True:
        for window, thread_id in THREAD_MAP.items():
            try:
                pane = capture_pane(window)
                if not pane:
                    continue

                ws = state[window]

                if is_waiting(pane):
                    context = extract_prompt_context(pane)
                    now = time.monotonic()
                    content_changed = context != ws["last_context"]
                    cooldown_expired = (now - ws["alerted_at"]) > MIN_REALERT_S

                    if ws["alert_msg_id"] is None or content_changed or cooldown_expired:
                        # Delete stale alert if content changed
                        if ws["alert_msg_id"] and content_changed:
                            delete_message(thread_id, ws["alert_msg_id"], tok)
                            ws["alert_msg_id"] = None

                        msg_text = f"⏳ **{window}** is waiting for input:\n```\n{context[:800]}\n```"
                        msg_id = post_alert(thread_id, msg_text, tok)
                        if msg_id:
                            ws["alert_msg_id"] = msg_id
                            ws["last_context"] = context
                            ws["alerted_at"] = now
                            print(f"[{window}] alerted (msg={msg_id})", flush=True)

                else:
                    # Cursor gone — clear the alert
                    if ws["alert_msg_id"]:
                        delete_message(thread_id, ws["alert_msg_id"], tok)
                        print(f"[{window}] cleared alert (msg={ws['alert_msg_id']})", flush=True)
                        ws["alert_msg_id"] = None
                        ws["last_context"] = ""

            except Exception as e:
                print(f"[{window}] error: {e}", flush=True)

        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
