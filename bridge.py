"""
cc-discord-bridge — deterministic Discord↔tmux relay.

NO LLM, no eval, no dynamic behavior.
SECURITY-CRITICAL: treats all forwarded content as hostile bytes.
Secrets never appear in argv, logs, or Discord output.

Configuration: copy config.example.py to config.py and fill in your values.
"""

__version__ = "1.1.2"

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# CONFIG — load from config.py (copy config.example.py and fill in)
# ---------------------------------------------------------------------------

try:
    import config as _cfg
except ImportError:
    print("ERROR: config.py not found. Copy config.example.py to config.py and fill in your values.", file=sys.stderr)
    sys.exit(1)

GUILD_ID        = _cfg.GUILD_ID
OWNER_ID        = _cfg.OWNER_ID          # Only this Discord user's messages are honored
CCODE_THREAD_ID = _cfg.CCODE_THREAD_ID   # Home channel for startup/heartbeat announcements
CHANNELS_CFG    = _cfg.CHANNELS           # List of {name, thread_id, tmux_target}

# SSH connection to the machine running tmux
MBPM5_SSH = [
    "ssh",
    "-i", os.path.expanduser(_cfg.SSH_KEY_PATH),
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=6",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    _cfg.SSH_TARGET,
]

TOKEN_ENV_FILE = _cfg.TOKEN_ENV_FILE      # Path to file containing DISCORD_BOT_TOKEN_CC=...
STATE_FILE     = os.path.expanduser("~/.cc-bridge/state.json")
API            = "https://discord.com/api/v10"

POLL_INTERVAL_S   = 2.5
HEARTBEAT_EVERY_S = 24 * 60 * 60   # daily

# Token-bucket: max 1/sec forwarded, burst 5
BUCKET_CAPACITY = 5
BUCKET_RATE     = 1.0   # tokens per second

# Bridge bot's own Discord user ID — used to detect @-mentions in mention-only mode.
BRIDGE_BOT_ID = _cfg.BRIDGE_BOT_ID

# ---------------------------------------------------------------------------
# Logging — never include message content
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("cc-bridge")


# ---------------------------------------------------------------------------
# Token loader
# ---------------------------------------------------------------------------

def _load_token() -> str:
    """Parse TOKEN_ENV_FILE for export DISCORD_BOT_TOKEN_CC=...
    Never logs the value."""
    path = Path(TOKEN_ENV_FILE)
    if not path.exists():
        raise RuntimeError(f"Token env file not found: {TOKEN_ENV_FILE}")
    pattern = re.compile(
        r"""^(?:export\s+)?DISCORD_BOT_TOKEN_CC\s*=\s*['"]?([^'"\s]+)['"]?""",
        re.MULTILINE,
    )
    text = path.read_text()
    m = pattern.search(text)
    if not m:
        raise RuntimeError("DISCORD_BOT_TOKEN_CC not found in token env file")
    return m.group(1)


# ---------------------------------------------------------------------------
# Token bucket rate limiter (defined before Channel dataclass)
# ---------------------------------------------------------------------------

class TokenBucket:
    def __init__(self, capacity: float, rate: float) -> None:
        self._capacity = capacity
        self._rate = rate        # tokens per second
        self._tokens = float(capacity)
        self._last = time.monotonic()

    def consume(self) -> bool:
        """Return True and consume a token if available, else False."""
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


# ---------------------------------------------------------------------------
# Channel definition — one per Discord thread / tmux window pair
# ---------------------------------------------------------------------------

@dataclass
class Channel:
    name: str
    thread_id: str
    tmux_target: str   # e.g. "mysession:MyWindow"
    last_seen: str = "0"
    locked: bool = False
    mention_only: bool = False
    forward_bots: frozenset = field(default_factory=frozenset)  # bot IDs allowed past OWNER_ID filter
    source_prefix: str = ""    # prepended to messages from forward_bots
    mirror_channel: str = ""   # if set, echo forwarded bot messages to this Discord channel ID
    bucket: TokenBucket = field(default_factory=lambda: TokenBucket(BUCKET_CAPACITY, BUCKET_RATE))


CHANNELS = [
    Channel(
        c["name"], c["thread_id"], c["tmux_target"],
        mention_only=bool(c.get("mention_only", False)),
        forward_bots=frozenset(c.get("forward_bots", ())),
        source_prefix=c.get("source_prefix", ""),
        mirror_channel=c.get("mirror_channel", ""),
    )
    for c in CHANNELS_CFG
]

CCODE_CHANNEL = CHANNELS[0]


# ---------------------------------------------------------------------------
# State persistence — per-channel
# ---------------------------------------------------------------------------

def _load_state() -> None:
    """Load persisted state into CHANNELS in-place."""
    p = Path(STATE_FILE)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        log.warning("Failed to load state (%s); starting fresh", exc)
        return

    # New multi-channel format: {"channels": {"CCode": {"last_seen": "...", "locked": bool}}}
    if "channels" in data:
        ch_data = data["channels"]
        for ch in CHANNELS:
            if ch.name in ch_data:
                ch.last_seen     = str(ch_data[ch.name].get("last_seen", "0"))
                ch.locked        = bool(ch_data[ch.name].get("locked", False))
                # Don't restore mention_only for source channels (forward_bots set) —
                # their mention gate is config, not runtime state.
                if not ch.forward_bots:
                    ch.mention_only  = bool(ch_data[ch.name].get("mention_only", False))
    # Legacy single-channel format: {"last_seen": "...", "locked": bool}
    elif "last_seen" in data:
        CCODE_CHANNEL.last_seen = str(data.get("last_seen", "0"))
        CCODE_CHANNEL.locked    = bool(data.get("locked", False))
        log.info("Migrated legacy state for first channel: last_seen=%s", CCODE_CHANNEL.last_seen)


def _save_state() -> None:
    """Persist all channel state atomically."""
    p = Path(STATE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    ch_data = {ch.name: {"last_seen": ch.last_seen, "locked": ch.locked, "mention_only": ch.mention_only} for ch in CHANNELS}
    tmp.write_text(json.dumps({"channels": ch_data}))
    tmp.replace(p)


# ---------------------------------------------------------------------------
# REDACT helper
# Apply to ANY pane-derived text before posting to Discord.
# ---------------------------------------------------------------------------

_REDACT_PATTERNS = [
    # SSH private key blocks
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    # Authorization Bearer / Bot headers
    re.compile(r"(Authorization\s*:\s*(?:Bearer|Bot)\s+)\S+", re.IGNORECASE),
    # Assignment patterns: *_API_KEY=, *TOKEN=, *SECRET=, *PASSWORD=
    re.compile(
        r"((?:[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Z0-9_]*)\s*=\s*)\S+",
        re.IGNORECASE,
    ),
    # AWS-style access key IDs (AKIA…)
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_…)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    # Slack tokens (xoxb-/xoxp-…)
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # Discord bot/user token shape (id.timestamp.hmac)
    re.compile(r"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}\b"),
    # High-entropy standalone strings > 40 chars (base64-ish / hex-ish)
    re.compile(r"(?<!\w)[A-Za-z0-9+/=_\-]{41,}(?!\w)"),
]


def _redact_repl(m: re.Match) -> str:
    if m.lastindex and m.lastindex >= 1:
        return m.group(1) + "«redacted»"
    return "«redacted»"


def redact(text: str) -> str:
    """Apply all redaction patterns to text. Safe to call on any pane output."""
    for pat in _REDACT_PATTERNS:
        try:
            text = pat.sub(_redact_repl, text)
        except Exception:
            try:
                text = pat.sub("«redacted»", text)
            except Exception:
                pass
    return text


# ---------------------------------------------------------------------------
# Discord HTTP helpers
# ---------------------------------------------------------------------------

class DiscordClient:
    def __init__(self, token: str) -> None:
        self._token = token
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bot {self._token}",
            "User-Agent": "cc-discord-bridge/2.0 (relay-only)",
            "Content-Type": "application/json",
        })

    def _handle_rate_limit(self, resp: requests.Response) -> None:
        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            retry_after = min(retry_after, 30.0)   # never strand on a huge backoff
            log.warning("Discord 429 — sleeping %.2fs", retry_after)
            time.sleep(retry_after)

    def get_messages(self, channel_id: str, after: str) -> list[dict]:
        url = f"{API}/channels/{channel_id}/messages"
        params: dict[str, Any] = {"after": after, "limit": 100}
        for _attempt in range(4):
            try:
                resp = self._session.get(url, params=params, timeout=10)
            except requests.RequestException as exc:
                log.error("GET messages network error: %s", exc)
                return []
            if resp.status_code == 429:
                self._handle_rate_limit(resp)
                continue
            if resp.status_code != 200:
                log.error("GET messages HTTP %d chan=%s", resp.status_code, channel_id)
                return []
            return sorted(resp.json(), key=lambda m: int(m["id"]))
        log.error("GET messages: rate-limit retry budget exhausted")
        return []

    def get_latest_id(self, channel_id: str) -> Optional[str]:
        """Return the id of the most recent message in the channel, or None."""
        url = f"{API}/channels/{channel_id}/messages"
        try:
            resp = self._session.get(url, params={"limit": 1}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return str(data[0]["id"]) if data else None
            log.error("get_latest_id HTTP %d chan=%s", resp.status_code, channel_id)
        except Exception as exc:
            log.error("get_latest_id error: %s", exc)
        return None

    def post_message(self, channel_id: str, content: str) -> Optional[str]:
        """Post a message; return message id on success, None on failure."""
        url = f"{API}/channels/{channel_id}/messages"
        payload = {"content": content, "allowed_mentions": {"parse": []}}
        for _attempt in range(4):
            try:
                resp = self._session.post(url, json=payload, timeout=10)
            except requests.RequestException as exc:
                log.error("POST message network error: %s", exc)
                return None
            if resp.status_code == 429:
                self._handle_rate_limit(resp)
                continue
            if resp.status_code not in (200, 201):
                log.error("POST message HTTP %d", resp.status_code)
                return None
            return str(resp.json().get("id"))
        log.error("POST message: rate-limit retry budget exhausted")
        return None

    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        import urllib.parse
        encoded = urllib.parse.quote(emoji, safe="")
        url = f"{API}/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
        for _attempt in range(4):
            try:
                resp = self._session.put(url, timeout=10)
            except requests.RequestException as exc:
                log.error("PUT reaction network error: %s", exc)
                return
            if resp.status_code == 429:
                self._handle_rate_limit(resp)
                continue
            if resp.status_code not in (200, 201, 204):
                log.warning("Reaction HTTP %d (emoji=%s)", resp.status_code, emoji)
            return


# ---------------------------------------------------------------------------
# SSH / tmux helpers
# ---------------------------------------------------------------------------

def _ssh_run(extra_args: list[str], input_bytes: Optional[bytes] = None, timeout: int = 20) -> subprocess.CompletedProcess:
    """Run a command over SSH. Command args are NEVER assembled as a shell string."""
    return subprocess.run(
        MBPM5_SSH + extra_args,
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
    )


def ssh_check() -> bool:
    try:
        return _ssh_run(["true"]).returncode == 0
    except Exception:
        return False


def tmux_check() -> bool:
    tmux_session = _cfg.TMUX_SESSION
    try:
        return _ssh_run(["tmux", "has-session", "-t", tmux_session]).returncode == 0
    except Exception:
        return False


def tmux_forward(content: bytes, tmux_target: str) -> tuple[bool, str]:
    """
    Forward raw bytes to a specific tmux window on the remote host.
    Returns (success, error_message).
    SECURITY: content is passed via stdin / list args — never interpolated into a shell.
    """
    try:
        r1 = _ssh_run(["tmux", "load-buffer", "-"], input_bytes=content)
        if r1.returncode != 0:
            return False, f"load-buffer failed (rc={r1.returncode})"

        r2 = _ssh_run(["tmux", "paste-buffer", "-t", tmux_target])
        if r2.returncode != 0:
            return False, f"paste-buffer failed (rc={r2.returncode})"

        r3 = _ssh_run(["tmux", "send-keys", "-t", tmux_target, "Enter"])
        if r3.returncode != 0:
            return False, f"send-keys failed (rc={r3.returncode})"

        return True, ""

    except subprocess.TimeoutExpired:
        return False, "ssh timeout"
    except Exception as exc:
        return False, f"unexpected error ({type(exc).__name__})"


def tmux_capture_pane(tmux_target: str) -> str:
    """Capture last 40 lines of the given tmux pane and redact."""
    try:
        result = _ssh_run(["tmux", "capture-pane", "-p", "-t", tmux_target], timeout=15)
        if result.returncode != 0:
            return "(capture-pane failed)"
        raw = result.stdout.decode(errors="replace")
        lines = raw.splitlines()
        snippet = "\n".join(lines[-40:]) if len(lines) > 40 else raw
        return redact(snippet).replace("```", "ʼʼʼ")
    except subprocess.TimeoutExpired:
        return "(capture-pane timed out)"
    except Exception as exc:
        return f"(capture-pane error: {type(exc).__name__})"


def tmux_list_windows() -> str:
    tmux_session = _cfg.TMUX_SESSION
    try:
        result = _ssh_run(
            ["tmux", "list-windows", "-t", tmux_session, "-F", "#{window_index} #{window_name}"],
            timeout=15,
        )
        if result.returncode != 0:
            return "(list-windows failed)"
        return result.stdout.decode(errors="replace").strip()
    except subprocess.TimeoutExpired:
        return "(list-windows timed out)"
    except Exception as exc:
        return f"(list-windows error: {type(exc).__name__})"


# ---------------------------------------------------------------------------
# Target attestation + input sanitization
# ---------------------------------------------------------------------------

# Strip C0/C1 control chars EXCEPT tab(09) and newline(0a); also CR is dropped.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def tmux_target_is_claude(tmux_target: str) -> tuple[bool, str]:
    """Verify the target pane's foreground process is Claude Code, not a bare shell.
    SAFETY: if the session exited, the pane is a shell — forwarding would execute
    the user's Discord text as shell commands. Refuse in that case."""
    try:
        r = _ssh_run(
            ["tmux", "list-panes", "-t", tmux_target, "-F", "#{pane_current_command}"],
            timeout=10,
        )
        if r.returncode != 0:
            return (False, "tmux-pane-query-failed")
        lines = r.stdout.decode(errors="replace").strip().splitlines()
        cmd = lines[0].strip() if lines else ""
        if "claude" in cmd.lower():
            return (True, cmd)
        return (False, cmd or "empty")
    except Exception:
        return (False, "?")


def sanitize_input(text: str) -> str:
    """Drop CR + C0/C1 control chars (keep tab/newline) before pasting to tmux."""
    return _CTRL_RE.sub("", text.replace("\r", ""))


# ---------------------------------------------------------------------------
# Chunk helper for long posts
# ---------------------------------------------------------------------------

def _chunk_text(text: str, max_len: int = 1900) -> list[str]:
    chunks: list[str] = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


# ---------------------------------------------------------------------------
# Control command handler (channel-aware)
# ---------------------------------------------------------------------------

def handle_control(
    word: str,
    ch: Channel,
    discord: DiscordClient,
    last_input_ts: float,
    last_heartbeat_ts: float,
) -> None:
    cmd = word.lower().strip()

    if cmd == "!lock":
        ch.locked = True
        _save_state()
        discord.post_message(ch.thread_id, "🔒 forwarding LOCKED")
        log.info("Forwarding locked for %s", ch.name)

    elif cmd == "!unlock":
        ch.locked = False
        _save_state()
        discord.post_message(ch.thread_id, "🔓 forwarding UNLOCKED")
        log.info("Forwarding unlocked for %s", ch.name)

    elif cmd == "!status":
        now = time.monotonic()
        input_age = int(now - last_input_ts) if last_input_ts > 0 else -1
        hb_age    = int(now - last_heartbeat_ts) if last_heartbeat_ts > 0 else -1
        ssh_ok    = ssh_check()
        tmux_ok   = tmux_check()
        ch_lines  = [
            f"  {c.name}: {'LOCKED' if c.locked else 'ok'}{' mention-only' if c.mention_only else ''}"
            for c in CHANNELS
        ]
        lines = [
            "**bridge status**",
            f"- ssh: {'ok' if ssh_ok else 'FAIL'}",
            f"- tmux {_cfg.TMUX_SESSION}: {'ok' if tmux_ok else 'missing'}",
            f"- last input: {f'{input_age}s ago' if input_age >= 0 else 'never'}",
            f"- last heartbeat: {f'{hb_age}s ago' if hb_age >= 0 else 'never'}",
            "- poll-loop: alive",
            "- channels:",
        ] + ch_lines
        discord.post_message(ch.thread_id, "\n".join(lines))

    elif cmd == "!screen":
        text = tmux_capture_pane(ch.tmux_target)
        discord.post_message(ch.thread_id, f"**`{ch.tmux_target}` (last ~40 lines):**")
        for chunk in _chunk_text(text, 1850):
            discord.post_message(ch.thread_id, f"```\n{chunk}\n```")

    elif cmd == "!sessions":
        text = tmux_list_windows()
        discord.post_message(ch.thread_id, f"**tmux windows ({_cfg.TMUX_SESSION}):**\n```\n{text}\n```")

    elif cmd == "!restart":
        discord.post_message(ch.thread_id, "♻️ restarting bridge")
        log.info("!restart command received — exiting for systemd restart")
        sys.exit(0)

    elif cmd == "!revive":
        tmux_session = _cfg.TMUX_SESSION
        discord.post_message(ch.thread_id, f"🔄 reviving {ch.tmux_target}...")
        try:
            r_check = _ssh_run(["tmux", "has-session", "-t", tmux_session])
            if r_check.returncode != 0:
                first_window = CHANNELS[0].name
                r_new = _ssh_run(["tmux", "new-session", "-d", "-s", tmux_session, "-n", first_window])
                if r_new.returncode != 0:
                    discord.post_message(ch.thread_id, f"❌ tmux new-session failed (rc={r_new.returncode})")
                    return
                discord.post_message(ch.thread_id, "📦 tmux session recreated")
            r_pane = _ssh_run(
                ["tmux", "list-panes", "-t", ch.tmux_target, "-F", "#{pane_current_command}"]
            )
            pane_cmd = r_pane.stdout.decode(errors="replace").strip() if r_pane.returncode == 0 else ""
            if "claude" in pane_cmd.lower():
                discord.post_message(ch.thread_id, f"✅ {ch.name} already running (`{pane_cmd}`)")
                return
            r_launch = _ssh_run(
                ["tmux", "send-keys", "-t", ch.tmux_target, "claude", "Enter"]
            )
            if r_launch.returncode == 0:
                discord.post_message(ch.thread_id, f"✅ `claude` launched in {ch.tmux_target} — give it 10s to start")
            else:
                discord.post_message(ch.thread_id, f"❌ send-keys failed (rc={r_launch.returncode})")
        except Exception as exc:
            discord.post_message(ch.thread_id, f"❌ !revive error: {exc}")

    elif cmd == "!mention-only":
        ch.mention_only = not ch.mention_only
        _save_state()
        status = "ON" if ch.mention_only else "OFF"
        discord.post_message(
            ch.thread_id,
            f"{'🔕' if ch.mention_only else '🔔'} mention-only {status} for {ch.name} — "
            f"{'only @bot-mentioned messages forwarded' if ch.mention_only else 'all owner messages forwarded'}",
        )
        log.info("mention-only %s for %s", status, ch.name)

    else:
        log.info("Unknown control word for %s (no content logged)", ch.name)
        discord.post_message(
            ch.thread_id,
            "⚠️ Unknown command. Known: !lock !unlock !status !screen !sessions !restart !revive !mention-only",
        )


# ---------------------------------------------------------------------------
# Heartbeat (first channel only — avoids spamming all threads)
# ---------------------------------------------------------------------------

def post_heartbeat(discord: DiscordClient, last_input_ts: float) -> None:
    now = time.monotonic()
    ssh_ok  = ssh_check()
    tmux_ok = tmux_check()
    input_age_m = int((now - last_input_ts) / 60) if last_input_ts > 0 else -1
    locked = [c.name for c in CHANNELS if c.locked]
    locked_str = f"LOCKED({','.join(locked)})" if locked else "ok"
    text = (
        f"bridge alive — forwarding {locked_str}, "
        f"ssh {'ok' if ssh_ok else 'FAIL'}, "
        f"tmux {'ok' if tmux_ok else 'missing'}, "
        f"last input {f'{input_age_m}m ago' if input_age_m >= 0 else 'never'}"
    )
    discord.post_message(CCODE_THREAD_ID, text)
    log.info("Heartbeat posted")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("cc-discord-bridge v2 starting (%d channels)", len(CHANNELS))

    token = _load_token()
    discord = DiscordClient(token)
    _load_state()

    last_input_ts     = 0.0
    last_heartbeat_ts = 0.0

    # SAFETY: skip-to-now for each channel that has no watermark yet.
    # FAIL CLOSED: if we cannot establish + persist the head, exit(1) for systemd retry.
    needs_save = False
    for ch in CHANNELS:
        if ch.last_seen and ch.last_seen != "0":
            log.info("Channel %s: resuming from last_seen=%s", ch.name, ch.last_seen)
            continue
        latest = None
        for _ in range(5):
            latest = discord.get_latest_id(ch.thread_id)
            if latest is not None:
                break
            time.sleep(2)
        if latest is None:
            log.error("skip-to-now: cannot fetch head for %s — exiting (fail-closed)", ch.name)
            sys.exit(1)
        ch.last_seen = latest
        needs_save = True
        log.info("Channel %s: skip-to-now at %s", ch.name, ch.last_seen)

    if needs_save:
        try:
            _save_state()
        except Exception as exc:
            log.error("skip-to-now: cannot persist state (%s) — exiting (fail-closed)", exc)
            sys.exit(1)

    # Startup announcement — first channel thread only
    channel_names = ", ".join(ch.name for ch in CHANNELS)
    try:
        discord.post_message(
            CCODE_THREAD_ID,
            f"🟢 bridge v2 online — {len(CHANNELS)} channels active ({channel_names}). "
            "Re-send anything sent while I was down.",
        )
        log.info("Startup announcement posted")
    except Exception as exc:
        log.error("Startup post failed: %s", exc)

    last_heartbeat_ts = time.monotonic()

    while True:
        try:
            now = time.monotonic()

            # Heartbeat check (first channel thread only)
            if now - last_heartbeat_ts >= HEARTBEAT_EVERY_S:
                try:
                    post_heartbeat(discord, last_input_ts)
                except Exception as exc:
                    log.error("Heartbeat failed: %s", exc)
                last_heartbeat_ts = time.monotonic()

            # Poll all channels
            for ch in CHANNELS:
                messages = discord.get_messages(ch.thread_id, ch.last_seen)

                for msg in messages:
                    msg_id     = str(msg.get("id", ""))
                    author     = msg.get("author", {})
                    author_id  = str(author.get("id", ""))
                    is_bot     = bool(author.get("bot", False))
                    webhook_id = msg.get("webhook_id")
                    content    = msg.get("content") or ""

                    # Persist high-water mark BEFORE acting (at-most-once semantics:
                    # a crash mid-forward must never replay a shell command). Fail closed.
                    if msg_id and (not ch.last_seen or int(msg_id) > int(ch.last_seen)):
                        ch.last_seen = msg_id
                        try:
                            _save_state()
                        except Exception as exc:
                            log.error(
                                "state save failed (msg=%s ch=%s) — NOT acting (fail-closed): %s",
                                msg_id, ch.name, exc,
                            )
                            continue

                    # Drop filters — log reason, NEVER log content
                    if author_id != OWNER_ID and author_id not in ch.forward_bots:
                        log.debug("DROP msg=%s ch=%s reason=wrong_author", msg_id, ch.name)
                        continue
                    if is_bot and author_id not in ch.forward_bots:
                        log.debug("DROP msg=%s ch=%s reason=bot_author", msg_id, ch.name)
                        continue
                    if webhook_id:
                        log.debug("DROP msg=%s ch=%s reason=webhook", msg_id, ch.name)
                        continue

                    stripped   = content.strip()
                    is_literal = stripped.startswith("!!")

                    if stripped.startswith("!") and not is_literal and author_id not in ch.forward_bots:
                        word = stripped.split()[0]
                        log.info("Control command msg=%s ch=%s word=%s", msg_id, ch.name, word)
                        try:
                            handle_control(word, ch, discord, last_input_ts, last_heartbeat_ts)
                        except Exception as exc:
                            log.error("Control handler error (msg=%s ch=%s): %s", msg_id, ch.name, exc)
                        continue

                    if ch.mention_only:
                        mentioned_ids = {u.get("id", "") for u in msg.get("mentions", [])}
                        if BRIDGE_BOT_ID not in mentioned_ids:
                            # Fallback: check raw mention syntax in content for reply-pings
                            # and legacy <@!id> nickname-mention form.
                            if (f"<@{BRIDGE_BOT_ID}>" not in content
                                    and f"<@!{BRIDGE_BOT_ID}>" not in content):
                                log.debug("SKIP msg=%s ch=%s reason=no_mention", msg_id, ch.name)
                                continue

                    if ch.locked:
                        log.info("LOCKED — dropping msg=%s ch=%s", msg_id, ch.name)
                        discord.add_reaction(ch.thread_id, msg_id, "🔒")
                        continue

                    if not ch.bucket.consume():
                        log.warning("Rate limit hit — dropping msg=%s ch=%s", msg_id, ch.name)
                        discord.add_reaction(ch.thread_id, msg_id, "⏳")
                        continue

                    # SAFETY: refuse to forward unless the pane is a live Claude Code session
                    target_ok, pane_cmd = tmux_target_is_claude(ch.tmux_target)
                    if not target_ok:
                        log.warning(
                            "target not claude (pane=%s) ch=%s — refusing msg=%s",
                            pane_cmd, ch.name, msg_id,
                        )
                        discord.add_reaction(ch.thread_id, msg_id, "⚠️")
                        discord.post_message(
                            ch.thread_id,
                            f"⚠️ {ch.name} session not running (pane=`{pane_cmd}`) — "
                            "not forwarding. Use `!screen` or `!revive`.",
                        )
                        continue

                    last_input_ts = time.monotonic()
                    log.info("Forwarding msg=%s to %s", msg_id, ch.tmux_target)
                    fwd = content.replace("!", "", 1) if is_literal else content
                    if ch.source_prefix and author_id in ch.forward_bots:
                        if not fwd.strip():
                            log.debug("DROP msg=%s ch=%s reason=empty_bot_content", msg_id, ch.name)
                            continue
                        fwd = ch.source_prefix + ":\n" + fwd
                    raw_bytes = sanitize_input(fwd).encode("utf-8")
                    ok, err = tmux_forward(raw_bytes, ch.tmux_target)
                    if ok:
                        discord.add_reaction(ch.thread_id, msg_id, "👀")
                        if ch.mirror_channel and author_id in ch.forward_bots:
                            mirror_text = redact(fwd)
                            try:
                                for chunk in _chunk_text(mirror_text, 1900):
                                    discord.post_message(ch.mirror_channel, chunk)
                            except Exception as exc:
                                log.warning("mirror post failed ch=%s: %s", ch.name, exc)
                    else:
                        log.error("tmux forward failed msg=%s ch=%s err=%s", msg_id, ch.name, err)
                        discord.add_reaction(ch.thread_id, msg_id, "❌")
                        discord.post_message(ch.thread_id, f"⚠️ forward failed: {err}")

        except Exception as exc:
            log.error("Main loop exception: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
