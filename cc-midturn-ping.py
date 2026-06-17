#!/usr/bin/env python3
"""PostToolUse hook: relay new assistant text to Discord in real-time.

After each tool call:
  - If new assistant text exists since last post → spawn background poster (text mode)
  - If no new text AND >5 min silent → spawn fallback "still working" ping (ping mode)

Exits in <1s. All posting is fire-and-forget. Fail-open and silent.
"""
from __future__ import annotations
import json, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cc_hook_utils import (
    current_turn_assistant_text, read_bridge_thread, wait_stable,
    char_count_file, lock_file, ping_stamp_file,
)

PING_COOLDOWN_S = 300.0
LOCK_TTL_S = 15.0


def _read_char_count(thread: str) -> int:
    try:
        return int(open(char_count_file(thread)).read().strip())
    except Exception:
        return 0


def _acquire_lock(thread: str) -> bool:
    """Atomic lock with TTL. Returns True if acquired."""
    lf = lock_file(thread)
    now = time.time()
    # Remove stale lock
    try:
        if now - os.path.getmtime(lf) < LOCK_TTL_S:
            return False  # held and fresh
        os.unlink(lf)
    except FileNotFoundError:
        pass
    except Exception:
        return False
    # Atomic create
    try:
        fd = os.open(lf, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(now).encode())
        os.close(fd)
        return True
    except (FileExistsError, OSError):
        return False


def _should_fallback_ping(thread: str) -> bool:
    now = time.monotonic()
    try:
        last = float(open(ping_stamp_file(thread)).read().strip())
        return now - last >= PING_COOLDOWN_S
    except Exception:
        return True


def _spawn_text_poster(thread: str, chunk: str, new_count: int) -> None:
    poster = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cc-midturn-post.py")
    if not os.path.exists(poster):
        return
    try:
        proc = subprocess.Popen(
            [sys.executable, poster, "text", thread, str(new_count)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        proc.stdin.write(chunk.encode("utf-8", errors="replace"))
        proc.stdin.close()
    except Exception:
        pass


def _spawn_fallback_ping(thread: str) -> None:
    try:
        open(ping_stamp_file(thread), "w").write(str(time.monotonic()))
    except Exception:
        return
    poster = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cc-midturn-post.py")
    if not os.path.exists(poster):
        return
    try:
        subprocess.Popen(
            [sys.executable, poster, "ping", thread],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def main() -> None:
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        pass

    thread = read_bridge_thread()
    if not thread:
        return

    tpath = ""
    try:
        tpath = json.loads(raw).get("transcript_path", "")
    except Exception:
        pass

    if not tpath or not os.path.exists(tpath):
        if _should_fallback_ping(thread):
            _spawn_fallback_ping(thread)
        return

    # Brief stability wait — transcript may still be mid-write
    wait_stable(tpath, stable_s=0.3, timeout_s=1.0)

    full_text = current_turn_assistant_text(tpath)  # raw, un-redacted
    last_count = _read_char_count(thread)
    new_chunk = full_text[last_count:]

    if new_chunk.strip():
        if not _acquire_lock(thread):
            return  # another poster in flight — next tool call will catch remainder
        _spawn_text_poster(thread, new_chunk, last_count + len(new_chunk))
        # Real text resets the fallback-ping timer
        try:
            open(ping_stamp_file(thread), "w").write(str(time.monotonic()))
        except Exception:
            pass
    else:
        if _should_fallback_ping(thread):
            _spawn_fallback_ping(thread)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
