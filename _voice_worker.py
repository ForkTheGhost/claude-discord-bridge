#!/usr/bin/env python3
"""Background voice worker: summarize → TTS → post MP3 to Discord.

Called by cc-stophook-post.py as a fire-and-forget subprocess.
Args: <thread_id>
Stdin: full response body text (piped by caller).
Always exits 0 (fail-open).

Requires a running local LLM server (llama.cpp or MLX compatible with OpenAI
chat completions API) and a TTS server (Kokoro or similar).
Configure LLM_URL and LLM_MODEL in config.py (or via environment variables).

Voice is resolved per-window:
  ~/.config/cc-bridge-threads/<window_name>  — contains the Discord channel/thread ID
  ~/.config/cc-bridge-voices/<window_name>   — contains the TTS voice key
Falls back to TTS_VOICE_DEFAULT if no per-window config is found.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

TTS_URL           = os.environ.get("CC_BRIDGE_TTS_URL", "http://127.0.0.1:8085/v1/audio/speech")
TTS_VOICE_DEFAULT = "echo"   # maps to Kokoro bm_daniel (British male) in tts-server.py KOKORO_VOICE_MAP
VOICES_CONFIG_DIR = os.path.expanduser("~/.config/cc-bridge-voices")
API               = "https://discord.com/api/v10"
TOKEN_FILE        = os.path.expanduser("~/.config/cc-bridge-token")

# LLM configuration — loaded from config.py or environment variables
_LLM_URL   = os.environ.get("CC_BRIDGE_LLM_URL",   "http://127.0.0.1:8080/v1/chat/completions")
_LLM_MODEL = os.environ.get("CC_BRIDGE_LLM_MODEL",  "local-model")

try:
    import config as _cfg
    if hasattr(_cfg, "LLM_URL"):
        _LLM_URL = _cfg.LLM_URL
    if hasattr(_cfg, "LLM_MODEL"):
        _LLM_MODEL = _cfg.LLM_MODEL
except ImportError:
    pass

LLM_URL   = _LLM_URL
LLM_MODEL = _LLM_MODEL

SUMMARY_PROMPT = (
    "You are preparing an assistant's response to be read aloud by a text-to-speech engine. "
    "Your output must sound natural when spoken — a listener cannot see the text. "
    "\n\n"
    "EXPAND all abbreviations and units into full spoken words: "
    "GB → gigabytes, MB → megabytes, KB → kilobytes, TB → terabytes, "
    "ms → milliseconds, s → seconds (when a unit), "
    "RSS → resident memory, PID → process ID, "
    "HTTP 200 → HTTP status 200 OK, HTTP 000 → connection failed. "
    "Write out numbers with their units naturally (e.g. '1.1 seconds', '16 kilobytes', '92 gigabytes'). "
    "\n\n"
    "REPLACE each code block, shell command, file path, error trace, diff, JSON snippet, YAML block, "
    "or script with ONE brief spoken sentence describing what it does or shows. "
    "Keep these replacement sentences short — one sentence each, no more. "
    "\n\n"
    "REPRODUCE all other prose verbatim, word for word. Do not summarize, shorten, or omit any prose. "
    "Do not pad, editorialize, or add commentary that wasn't in the original. "
    "Maintain first person where the original uses it. "
    "\n\n"
    "You MUST end at a complete sentence. "
    "Output only the spoken text, nothing else."
)


def _read_token() -> str:
    try:
        for line in open(TOKEN_FILE):
            m = re.match(r'\s*(?:export\s+)?DISCORD_BOT_TOKEN_CCODE\s*=\s*[\'"]?([^\s\'"]+)', line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def _resolve_voice(thread_id: str) -> str:
    """Return TTS voice key for the window that owns thread_id, or default.

    Scans ~/.config/cc-bridge-threads/ for a file whose content matches
    thread_id, then reads the matching ~/.config/cc-bridge-voices/<window>
    file for the voice key.
    """
    threads_dir = os.path.expanduser("~/.config/cc-bridge-threads")
    if os.path.isdir(threads_dir):
        for fname in os.listdir(threads_dir):
            if "/" in fname or fname.startswith("."):
                continue
            fpath = os.path.join(threads_dir, fname)
            try:
                if open(fpath).read().strip() == thread_id:
                    voice_file = os.path.join(VOICES_CONFIG_DIR, fname)
                    if os.path.isfile(voice_file):
                        return open(voice_file).read().strip()
            except Exception:
                pass
    return TTS_VOICE_DEFAULT


def _summarize(body: str) -> str:
    """Prepare text for TTS: verbatim prose, technical noise replaced with descriptions.
    Returns original excerpt on failure."""
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user",   "content": body[:16000] + "\n\n/no_think"},
        ],
        "max_tokens": 1500,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        LLM_URL, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
            choice = data["choices"][0]
            text = choice["message"]["content"].strip()
            text = re.sub(r'^\s*<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()
            if not text:
                return body[:2000]
            # If LLM hit token limit mid-sentence, truncate to last complete sentence
            if choice.get("finish_reason") == "length" or text[-1] not in '.!?':
                # Lookahead avoids false matches on decimals (3.5), versions (2.0.1)
                m = re.search(r'^(.*[.!?])(?=\s+[A-Z]|\s*$)', text, re.DOTALL)
                if m:
                    text = m.group(1).strip()
            return text
    except Exception:
        return body[:2000]


def _tts(text: str, voice: str = TTS_VOICE_DEFAULT) -> bytes | None:
    """Generate MP3 via TTS server. Returns None on failure."""
    payload = json.dumps({
        "model": "tts-1",
        "input": text[:8000],
        "voice": voice,
        "response_format": "mp3",
    }).encode()
    req = urllib.request.Request(
        TTS_URL, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            audio = r.read()
            return audio if len(audio) > 500 else None
    except Exception:
        return None


def _resample_44k(audio: bytes, ffmpeg: str = "/opt/homebrew/bin/ffmpeg") -> bytes:
    """Resample MP3 to MPEG-1 44.1 kHz for iOS/Safari compatibility.

    Kokoro TTS outputs MPEG-2 at 24 kHz which iOS AudioToolbox / Safari may
    refuse to play inline. ffmpeg converts to standard MPEG-1 44.1 kHz.
    Fail-open: returns original audio if ffmpeg is absent or conversion fails.
    """
    if not os.path.exists(ffmpeg):
        return audio
    in_path = out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fin:
            fin.write(audio)
            in_path = fin.name
        out_path = in_path + "_44k.mp3"
        result = subprocess.run(
            [ffmpeg, "-y", "-i", in_path, "-ar", "44100", "-b:a", "64k", out_path],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0 and os.path.getsize(out_path) > 500:
            with open(out_path, "rb") as f:
                return f.read()
    except Exception:
        pass
    finally:
        for p in (in_path, out_path):
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass
    return audio


def _post_audio(thread_id: str, audio: bytes, tok: str) -> None:
    boundary = "----ccvoice"
    pj = json.dumps({"allowed_mentions": {"parse": []}}).encode()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
        f"Content-Type: application/json\r\n\r\n".encode() + pj + b"\r\n" +
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
        f"filename=\"summary.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n".encode() +
        audio + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        f"{API}/channels/{thread_id}/messages",
        data=body, method="POST",
        headers={
            "Authorization": f"Bot {tok}",
            "User-Agent": "ccode-voice-worker",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    urllib.request.urlopen(req, timeout=30)


def main() -> None:
    if len(sys.argv) < 2:
        return
    thread_id = sys.argv[1]
    body = sys.stdin.read().strip()
    if not body:
        return

    tok = _read_token()
    if not tok:
        return

    # argv[2] allows caller (bridge.py) to override voice without config lookup
    voice = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else _resolve_voice(thread_id)
    summary = _summarize(body)
    audio = _tts(summary, voice)
    if not audio:
        return

    audio = _resample_44k(audio)

    try:
        _post_audio(thread_id, audio, tok)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
