# Changelog

## [1.1.4] — 2026-06-18

### Added
- **`forward_voice` channel config field**: Source channels (e.g. `ArdI-src`, `Palo-src`) can now specify a TTS voice key via `"forward_voice": "ardi"` in config. When a `forward_bots` message is mirrored to another channel, the bridge spawns `_voice_worker.py` with that voice, so ArdI and Palo can speak in their own distinct voices (F5 clones) rather than Claudius's default.
- **`_spawn_voice()` helper in `bridge.py`**: Fire-and-forget subprocess that passes optional voice override to the voice worker.
- **`_voice_worker.py` voice override arg**: Accepts optional `argv[2]` voice key; if provided, skips `_resolve_voice()` config lookup. This lets the bridge caller dictate the voice without needing per-thread config files.

## [1.1.3] — 2026-06-06

### Added
- **`_voice_worker.py` — TTS voice routing per window**: `_resolve_voice()` scans `~/.config/cc-bridge-threads/` to find the window owning a thread ID, then reads `~/.config/cc-bridge-voices/<window>` for the voice key. Falls back to `TTS_VOICE_DEFAULT` (`"echo"`) if unconfigured.
- **`_resample_44k()`**: Converts Kokoro's 24 kHz MPEG-2 MP3 output to 44.1 kHz MPEG-1 via ffmpeg for iOS/Safari playback compatibility. Fail-open — returns original audio if ffmpeg is absent or conversion fails.
- **F5-TTS voice support**: `_tts()` passes the voice key directly to the TTS server, enabling custom voice clones (`"ardi"`, `"palo"`) alongside Kokoro voices (`"echo"`, `"onyx"`, etc.).
- **LLM config via `config.py` or env vars**: `LLM_URL` and `LLM_MODEL` can be set in `config.py` or via `CC_BRIDGE_LLM_URL` / `CC_BRIDGE_LLM_MODEL` environment variables. Defaults to `http://127.0.0.1:8080/v1/chat/completions`.

## [1.1.2] — 2026-06-18

### Fixed
- **🔕 reaction removed from mention_only path**: Messages that fail the @mention check are now silently dropped with no reaction. The 🔕 was noise — un-mentioned messages from any author (bots, users) are simply ignored. Forward failures still produce ❌.

## [1.1.1] — 2026-06-18

### Fixed
- **🔕 reaction noise**: `mention_only` channels were reacting with 🔕 to *every* non-mentioning message (other users, bots, random traffic). Now only messages from a `forward_bots` member that failed the @mention check receive the 🔕 reaction — the intended signal that a trusted bot tried to reach CC but didn't include the mention.

## [1.1.0] — 2026-06-18

### Added
- **Bot-source channel routing** (`forward_bots`, `source_prefix`, `mirror_channel` on `Channel`):
  - `forward_bots`: frozenset of bot Discord user IDs allowed to bypass the `OWNER_ID` filter on a per-channel basis. Empty by default — existing channels are unchanged.
  - `source_prefix`: string prepended to forwarded bot messages in tmux so the session knows the origin (e.g. `"From ArdI on Discord #ardi"`).
  - `mirror_channel`: Discord channel ID; when set, a redacted copy of each forwarded bot message is also posted to that channel. Chunked to ≤1900 chars. Fail-open.
  - Config example added to `config.example.py`.
- **`_load_state()` hardening**: `mention_only` is no longer restored from persisted state for channels with `forward_bots` set. Their mention gate is config-only and cannot be silently disabled by a toggle + restart.
- **Empty bot message guard**: bot messages with empty/whitespace content are dropped before the source prefix is applied (`reason=empty_bot_content`).

### Changed
- **Author filter**: extended to allow `forward_bots` members through the `OWNER_ID` and `is_bot` drop checks.
- **Control command guard**: `forward_bots` members cannot issue `!` control commands.
- **`cc-midturn-ping.py`**: removed the 5-minute fallback "Still working on this" ping. Mid-turn text chunks (real assistant output) continue to post in real-time; the silent fallback ping for long tool-call chains is gone.
- **`CHANNELS` comprehension**: updated to pass optional `mention_only`, `forward_bots`, `source_prefix`, `mirror_channel` from config dicts.

### Fixed
- State restore could override a hardcoded `mention_only=True` on source channels to `False` after a toggle + restart (Opus audit finding).

## [1.0.0] — 2026-06-01

- Initial release: per-window TTS voice resolution, multi-channel bridging.
