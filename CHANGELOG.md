# Changelog

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
