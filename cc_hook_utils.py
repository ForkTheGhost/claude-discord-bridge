#!/usr/bin/env python3
"""Shared utilities for Claude Code Discord bridge hooks.

Import via:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cc_hook_utils import ...
"""
from __future__ import annotations
import json, os, re, subprocess, time

DISCORD_MSG_MAX = 1900
MAX_CHUNKS = 8

# Designates that a message is still in-progress (more content coming).
# Change this one constant to change the indicator everywhere.
WORKING_EMOJI = "⚙️"

TRUNCATION_MARKER = "…(see full response in tmux)"

_REDACT = [
    re.compile(r"(?i)(authorization:\s*bearer)\s+\S+"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD))\s*[=:]\s*\S+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b"),
]


def redact(text: str) -> str:
    for pat in _REDACT:
        text = pat.sub(
            lambda m: (m.group(1) + " «redacted»") if m.groups() else "«redacted»", text
        )
    return text


def split_for_discord(text: str) -> list:
    """Split text into ≤1900-char Discord chunks at sentence/word boundaries."""
    if len(text) <= DISCORD_MSG_MAX:
        return [text]
    chunks = []
    while text and len(chunks) < MAX_CHUNKS:
        if len(text) <= DISCORD_MSG_MAX:
            chunks.append(text)
            text = ""
            break
        cut = text[:DISCORD_MSG_MAX]
        split_at = max(
            cut.rfind(".\n"), cut.rfind("!\n"), cut.rfind("?\n"),
            cut.rfind(". "),  cut.rfind("! "),  cut.rfind("? "),
            cut.rfind("\n"),  cut.rfind(" "),
        )
        if split_at > DISCORD_MSG_MAX // 2:
            chunks.append(text[:split_at + 1].rstrip())
            text = text[split_at + 1:].lstrip()
        else:
            chunks.append(cut)
            text = text[DISCORD_MSG_MAX:]
    if text:
        chunks.append(TRUNCATION_MARKER)
    return chunks


def current_turn_assistant_text(transcript_path: str) -> str:
    """Return raw (un-redacted) assistant text for the current turn only.

    Anchors on the last stop_hook_summary entry so multi-turn sessions don't
    re-post stale content. Returns "" on any error (fail-open).
    """
    entries = []
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return ""

    # Anchor: last stop_hook_summary marks end of previous turn
    last_hook_idx = -1
    for i, obj in enumerate(entries):
        if obj.get("type") == "system" and obj.get("subtype") == "stop_hook_summary":
            last_hook_idx = i
    search_start = last_hook_idx + 1

    # Find last real user message after the anchor
    last_user_idx = -1
    for i, obj in enumerate(entries[search_start:], search_start):
        role = obj.get("type", obj.get("role", ""))
        if role != "user":
            continue
        msg = obj.get("message", obj)
        content = msg.get("content", [])
        if isinstance(content, str) and content.strip():
            last_user_idx = i
        elif isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
            for b in content
        ):
            last_user_idx = i

    anchor = max(search_start, last_user_idx + 1) if last_user_idx >= 0 else search_start
    if anchor >= len(entries):
        return ""

    # Collect assistant text after the anchor
    parts = []
    for obj in entries[anchor:]:
        role = obj.get("type", obj.get("role", ""))
        if role != "assistant":
            continue
        msg = obj.get("message", obj)
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    t = b.get("text", "").strip()
                    if t:
                        parts.append(t)
    return "\n\n".join(parts)


def read_bridge_thread() -> str:
    """Thread file first (dynamically updated); fall back to override file, then env var.

    Priority:
      1. TMUX_PANE → ~/.config/cc-bridge-threads/<window>  (per-window, authoritative)
      2. ~/.config/cc-bridge-thread (singular override file — updated on channel migration)
      3. CC_BRIDGE_THREAD env var (last resort; may be stale after channel migration)
    File sources take precedence so channel migrations take effect without restarting claude.
    """
    try:
        pane = os.environ.get("TMUX_PANE", "")
        if pane:
            r = subprocess.run(
                ["tmux", "display-message", "-t", pane, "-p", "#{window_name}"],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode == 0:
                name = r.stdout.strip()
                thread_file = os.path.expanduser(f"~/.config/cc-bridge-threads/{name}")
                if os.path.exists(thread_file):
                    return open(thread_file).read().strip()
    except Exception:
        pass
    # Priority 2: singular override file (survives session restarts and channel migrations)
    try:
        override = os.path.expanduser("~/.config/cc-bridge-thread")
        if os.path.exists(override):
            val = open(override).read().strip()
            if val:
                return val
    except Exception:
        pass
    return os.environ.get("CC_BRIDGE_THREAD", "").strip()


def read_token() -> str:
    """Read CC bot token from ~/.config/cc-bridge-token or ~/.zshenv fallback."""
    try:
        for line in open(os.path.expanduser("~/.config/cc-bridge-token")):
            m = re.match(r'\s*(?:export\s+)?DISCORD_BOT_TOKEN_CCODE\s*=\s*[\'"]?([^\s\'"]+)', line)
            if m:
                return m.group(1)
    except Exception:
        pass
    try:
        for line in open(os.path.expanduser("~/.zshenv")):
            m = re.match(r'\s*export\s+DISCORD_BOT_TOKEN_CC\s*=\s*[\'"]?([^\s\'"]+)', line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def wait_stable(path: str, stable_s: float = 0.4, timeout_s: float = 4.0) -> None:
    """Wait until path stops growing (stable for stable_s seconds)."""
    prev_size = -1
    stable_since = 0.0
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            size = os.path.getsize(path)
        except Exception:
            return
        now = time.time()
        if size != prev_size:
            prev_size = size
            stable_since = now
        elif now - stable_since >= stable_s:
            return
        time.sleep(0.05)


# --- Per-thread state file paths ---

def char_count_file(thread: str) -> str:
    return f"/tmp/cc-hook-posted-chars-{thread}"

def inprogress_file(thread: str) -> str:
    return f"/tmp/cc-hook-inprogress-{thread}"

def lock_file(thread: str) -> str:
    return f"/tmp/cc-hook-midturn-lock-{thread}"

def ping_stamp_file(thread: str) -> str:
    return f"/tmp/cc-hook-last-ping-{thread}"

def post_stamp_file(thread: str) -> str:
    return f"/tmp/cc-hook-last-post-{thread}"
