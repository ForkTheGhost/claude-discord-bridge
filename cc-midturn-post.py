#!/usr/bin/env python3
"""Background poster for mid-turn Discord messages.

Modes (argv):
  text {thread} {new_count}  — read raw chunk from stdin, redact, post with WORKING_EMOJI,
                               track message IDs for later cleanup, update char count, release lock
  ping {thread}              — post fixed "still working" text + optional audio (no emoji tracking)

Always exits 0. Fail-open.
"""
from __future__ import annotations
import json, os, sys, time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cc_hook_utils import (
    WORKING_EMOJI, TRUNCATION_MARKER,
    redact, split_for_discord, read_token,
    char_count_file, inprogress_file, lock_file,
)

PING_TEXT = f"{WORKING_EMOJI} Still working on this — full response coming soon."
PING_AUDIO_TEXT = "Still working on this. I'll have the full response soon."
TTS_URL = "http://127.0.0.1:8085/v1/audio/speech"
TTS_VOICE = "bm_daniel"
VOICE_TOGGLE = os.path.expanduser("~/.config/cc-bridge-voice")
API = "https://discord.com/api/v10"


def _release_lock(thread: str) -> None:
    try:
        os.unlink(lock_file(thread))
    except Exception:
        pass


def _post_message(thread: str, content: str, tok: str) -> str | None:
    """POST a message. Returns Discord message ID or None. Retries once on 429."""
    data = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(
        f"{API}/channels/{thread}/messages",
        data=data, method="POST",
        headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json",
                 "User-Agent": "ccode-midturn"},
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read()).get("id")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                try:
                    time.sleep(min(float(e.headers.get("Retry-After", "5")), 8))
                except Exception:
                    time.sleep(5)
                continue
            return None
        except Exception:
            return None
    return None


def _add_reaction(thread: str, msg_id: str, emoji: str, tok: str) -> bool:
    """Add emoji as a reaction to a posted message. Returns True on 204."""
    encoded = urllib.parse.quote(emoji, safe="")
    req = urllib.request.Request(
        f"{API}/channels/{thread}/messages/{msg_id}/reactions/{encoded}/@me",
        data=b"", method="PUT",
        headers={"Authorization": f"Bot {tok}", "User-Agent": "ccode-midturn"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status == 204
    except Exception:
        return False


def _append_inprogress(thread: str, msg_ids: list) -> None:
    """Append message IDs (strings) to inprogress tracking file."""
    fpath = inprogress_file(thread)
    try:
        existing = json.loads(open(fpath).read())
    except Exception:
        existing = []
    existing.extend(msg_ids)
    try:
        open(fpath, "w").write(json.dumps(existing))
    except Exception:
        pass


def _post_audio_ping(thread: str, tok: str) -> None:
    """Fixed-phrase "still working" audio attachment. Fail-open."""
    try:
        payload = json.dumps({
            "model": "kokoro", "input": PING_AUDIO_TEXT,
            "voice": TTS_VOICE, "response_format": "mp3",
        }).encode()
        req = urllib.request.Request(
            TTS_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            audio = r.read()
        if len(audio) < 500:
            return
        boundary = "----ccping"
        pj = json.dumps({"allowed_mentions": {"parse": []}}).encode()
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n".encode() + pj + b"\r\n" +
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
            f'filename="working.mp3"\r\nContent-Type: audio/mpeg\r\n\r\n'.encode() +
            audio + f"\r\n--{boundary}--\r\n".encode()
        )
        urllib.request.urlopen(
            urllib.request.Request(
                f"{API}/channels/{thread}/messages",
                data=body, method="POST",
                headers={"Authorization": f"Bot {tok}", "User-Agent": "ccode-midturn",
                         "Content-Type": f"multipart/form-data; boundary={boundary}"},
            ),
            timeout=30,
        )
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) < 3:
        return

    mode = sys.argv[1]
    thread = sys.argv[2]
    tok = read_token()
    if not tok:
        if mode == "text":
            _release_lock(thread)
        return

    if mode == "ping":
        _post_message(thread, PING_TEXT, tok)
        if os.path.exists(VOICE_TOGGLE):
            _post_audio_ping(thread, tok)
        return

    if mode == "text":
        if len(sys.argv) < 4:
            _release_lock(thread)
            return
        new_count = int(sys.argv[3])
        raw_chunk = sys.stdin.read()
        if not raw_chunk.strip():
            _release_lock(thread)
            return

        body = redact(raw_chunk).strip()
        if not body:
            _release_lock(thread)
            return

        chunks = split_for_discord(body)
        tracked_ids = []

        for chunk in chunks:
            is_truncation = chunk == TRUNCATION_MARKER
            msg_id = _post_message(thread, chunk, tok)
            if msg_id and not is_truncation:
                _add_reaction(thread, msg_id, WORKING_EMOJI, tok)
                tracked_ids.append(msg_id)

        if tracked_ids:
            _append_inprogress(thread, tracked_ids)
        try:
            open(char_count_file(thread), "w").write(str(new_count))
        except Exception:
            pass
        _release_lock(thread)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Ensure lock released on uncaught error in text mode
        if len(sys.argv) >= 3 and sys.argv[1] == "text":
            try:
                os.unlink(lock_file(sys.argv[2]))
            except Exception:
                pass
    sys.exit(0)
