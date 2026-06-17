#!/usr/bin/env python3
"""Claude Code Stop-hook → post the assistant's final reply to a Discord thread.

Runs on the machine running Claude Code (the tmux host) when any CC session
finishes a turn. Posts a condensed version of the reply to the matching
Discord thread.

Thread resolution: CC_BRIDGE_THREAD env var first; then TMUX_PANE →
~/.config/cc-bridge-threads/<window_name> fallback for sessions where the var
isn't set in the environment.

Fail-OPEN and SILENT: a hook must never break the session — any error skips post.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cc_hook_utils import (
    WORKING_EMOJI,
    redact as _redact_shared,
    split_for_discord as _split_shared,
    current_turn_assistant_text as _ctat_shared,
    read_bridge_thread as _read_bridge_thread_shared,
    read_token as _read_token_shared,
    wait_stable as _wait_stable_shared,
    char_count_file, inprogress_file, post_stamp_file,
)

TTS_URL = "http://127.0.0.1:8085/v1/audio/speech"
TTS_VOICE = "echo"   # maps to Kokoro bm_daniel in tts-server.py KOKORO_VOICE_MAP

_MIN_POST_INTERVAL_S = 3.0


def _stamp_path(thread: str) -> str:
    return post_stamp_file(thread)


def _throttle_check(stamp: str) -> bool:
    """Return True if OK to post. Does NOT update stamp — caller updates after success."""
    import time
    now = time.time()
    try:
        last = float(open(stamp).read().strip())
        if now - last < _MIN_POST_INTERVAL_S:
            return False
    except Exception:
        pass
    return True


def _update_stamp(stamp: str, offset: float = 0.0) -> None:
    """Write current time (+ optional future offset for rate-limit backoff) to stamp file."""
    import time
    try:
        open(stamp, "w").write(str(time.time() + offset))
    except Exception:
        pass


def current_turn_assistant_text(transcript_path: str) -> str:
    return _ctat_shared(transcript_path)


def _wait_stable(path: str, stable_s: float = 0.4, timeout_s: float = 4.0) -> None:
    _wait_stable_shared(path, stable_s, timeout_s)


def condense(text):
    return _redact_shared(text).strip()


def split_for_discord(text: str) -> list:
    return _split_shared(text)


def _post_chunk(thread: str, content: str, tok: str) -> bool:
    """Post one Discord message. Retries once on 429 (sleep capped at 8s). Returns True on 2xx."""
    import urllib.request as ureq
    import urllib.error as uerr
    import time

    def _attempt():
        data = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
        req = ureq.Request(
            f"https://discord.com/api/v10/channels/{thread}/messages",
            data=data, method="POST",
            headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json",
                     "User-Agent": "ccode-hook"},
        )
        return ureq.urlopen(req, timeout=12)

    try:
        with _attempt() as resp:
            return 200 <= resp.status < 300
    except uerr.HTTPError as e:
        if e.code == 429:
            try:
                retry_after = min(float(e.headers.get("Retry-After", "5")), 8)
            except Exception:
                retry_after = 5.0
            time.sleep(retry_after)
            try:
                with _attempt() as resp:
                    return 200 <= resp.status < 300
            except Exception:
                return False
        return False
    except Exception:
        return False


def _read_bridge_thread() -> str:
    return _read_bridge_thread_shared()


def _cleanup_midturn(thread: str, tok: str) -> None:
    """Remove WORKING_EMOJI reaction from in-progress messages, then clear state files.

    Always runs at turn end — even when throttled or when there's nothing new to post.
    If tok is empty, skips reaction deletes (reaction stays) but still clears state
    files so the next turn starts with a clean char count. Fail-open.
    """
    if tok:
        import urllib.request as _ureq
        import urllib.parse
        try:
            msg_ids = json.loads(open(inprogress_file(thread)).read())
        except Exception:
            msg_ids = []
        encoded = urllib.parse.quote(WORKING_EMOJI, safe="")
        for msg_id in msg_ids:
            try:
                req = _ureq.Request(
                    f"https://discord.com/api/v10/channels/{thread}/messages/{msg_id}/reactions/{encoded}/@me",
                    method="DELETE",
                    headers={"Authorization": f"Bot {tok}", "User-Agent": "ccode-hook"},
                )
                _ureq.urlopen(req, timeout=6)
            except Exception:
                pass

    # Always clear state files so next turn starts fresh
    for fp in (inprogress_file(thread), char_count_file(thread)):
        try:
            os.unlink(fp)
        except Exception:
            pass


def main():
    import time
    thread = _read_bridge_thread()
    if not thread:
        return

    # Get token early so cleanup can PATCH emojis on any exit path
    tok = _read_token()

    tpath = ""
    try:
        raw = sys.stdin.read()
        hook = json.loads(raw)
        tpath = hook.get("transcript_path", "")
    except Exception:
        _cleanup_midturn(thread, tok)
        return

    if not tpath or not os.path.exists(tpath):
        _cleanup_midturn(thread, tok)
        return

    if not tok:
        _cleanup_midturn(thread, tok)  # clears state files; can't PATCH without token
        return

    _wait_stable(tpath)

    # Compute delta: only post what midturn hooks haven't already posted
    raw_full = current_turn_assistant_text(tpath)  # un-redacted
    try:
        last_count = int(open(char_count_file(thread)).read().strip())
    except Exception:
        last_count = 0
    body = condense(raw_full[last_count:])  # redact + strip delta only

    stamp = _stamp_path(thread)

    if not body:
        _cleanup_midturn(thread, tok)
        return

    if not _throttle_check(stamp):
        _cleanup_midturn(thread, tok)
        return

    import urllib.request as _ureq
    chunks = split_for_discord(body)
    first_posted = False
    for i, chunk in enumerate(chunks):
        ok = _post_chunk(thread, chunk, tok)
        if i == 0:
            if not ok:
                _cleanup_midturn(thread, tok)
                return
            first_posted = True
            _update_stamp(stamp)
            _remove_forward_reaction(thread, tok, _ureq)
        elif not ok:
            break

    _cleanup_midturn(thread, tok)

    # Voice worker gets FULL redacted turn body (not just delta)
    full_body = condense(raw_full)
    if first_posted and os.path.exists(os.path.expanduser("~/.config/cc-bridge-voice")):
        import subprocess as _sp
        _worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_voice_worker.py")
        if os.path.exists(_worker):
            proc = _sp.Popen(
                [sys.executable, _worker, thread],
                stdin=_sp.PIPE, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                start_new_session=True,
            )
            proc.stdin.write(full_body.encode("utf-8", errors="replace"))
            proc.stdin.close()


# OWNER_ID is used to find the most recent message from the bridge owner,
# so we can remove the 👀 forwarding reaction once the reply is posted.
# Set this to the same value as OWNER_ID in config.py.
# This is loaded from the environment variable CC_BRIDGE_OWNER_ID at runtime.
_OWNER_ID = os.environ.get("CC_BRIDGE_OWNER_ID", "")


def _remove_forward_reaction(thread: str, tok: str, ureq_mod) -> None:
    """Remove the 👀 reaction from the owner's most recent message.
    The reply itself is the confirmation. Fail-open and silent."""
    if not _OWNER_ID:
        return
    try:
        req = ureq_mod.Request(
            f"https://discord.com/api/v10/channels/{thread}/messages?limit=10",
            headers={"Authorization": f"Bot {tok}", "User-Agent": "ccode-hook"},
        )
        with ureq_mod.urlopen(req, timeout=4) as r:
            msgs = json.loads(r.read())
        owner_msgs = [m for m in msgs if m.get("author", {}).get("id") == _OWNER_ID]
        if not owner_msgs:
            return
        msg_id = owner_msgs[0]["id"]
        import urllib.parse
        encoded = urllib.parse.quote("👀", safe="")
        del_req = ureq_mod.Request(
            f"https://discord.com/api/v10/channels/{thread}/messages/{msg_id}/reactions/{encoded}/@me",
            method="DELETE",
            headers={"Authorization": f"Bot {tok}", "User-Agent": "ccode-hook"},
        )
        ureq_mod.urlopen(del_req, timeout=4)
    except Exception:
        return  # fail-open


def _read_token() -> str:
    return _read_token_shared()


if __name__ == "__main__":
    main()
