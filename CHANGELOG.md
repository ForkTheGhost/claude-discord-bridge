# Changelog

## [1.1.4] — 2026-06-19

### Added
- **`_resample_44k()` in `_voice_worker.py`**: Converts Kokoro's 24 kHz MPEG-2 MP3 output to 44.1 kHz MPEG-1 via ffmpeg for iOS/Safari playback compatibility. Fail-open — returns original audio if ffmpeg is absent or conversion fails.
- **`argv[2]` voice override in `_voice_worker.py`**: Caller (`bridge.py`) can pass a voice key directly as a second argument, bypassing `_resolve_voice()` config lookup.

### Fixed
- **`read_bridge_thread()` stale env var fallback**: After a Discord channel migration, `CC_BRIDGE_THREAD` env var in long-running sessions could point to old thread IDs. Added `~/.config/cc-bridge-thread` (singular) as priority-2 fallback, read after TMUX_PANE lookup but before the env var. Protects against double-posting when TMUX_PANE lookup occasionally fails. Thread file is updated at migration time; env var is now last resort only.

## [1.1.3] — 2026-06-18

### Added
- **Per-agent TTS voice** (`forward_voice` on `Channel`): when a `forward_bots` message is mirrored, the bridge now optionally speaks it aloud using a per-channel voice key (e.g. `"am_adam"` for Claudius, `"ardi"` for ArdI). Requires `~/.config/cc-bridge-voice` sentinel file to be present (opt-in). Spawns `_voice_worker.py` as a fire-and-forget subprocess.
- **`CC_BRIDGE_TTS_URL` env var** in `_voice_worker.py`: allows pointing the TTS server at a remote host (e.g. `http://192.168.11.13:8085/v1/audio/speech` for ardymus-grid → MBPm5). Falls back to `http://127.0.0.1:8085`.

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
