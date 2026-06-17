# claude-discord-bridge

Deterministic Discord thread ↔ tmux relay for Claude Code sessions.

**No LLM, no eval, no dynamic behavior in the bridge process.**
Only messages from a configured owner Discord ID are ever forwarded.
All forwarded content is treated as hostile bytes and passed via stdin/argv lists — never interpolated into a shell string.

---

## Architecture

```
Discord thread
     │
     │  (poll every 2.5s via REST API)
     ▼
  bridge.py          ← runs on a relay host (e.g. Linux VPS or always-on machine)
     │
     │  (SSH, no shell interpolation)
     ▼
  tmux windows       ← cc-main:CCode, cc-main:CC-Infra, etc. on your local machine
     │
     │  (Claude Code stop hook, per-session)
     ▼
  cc-stophook-post.py  → posts CC reply back to Discord thread
  cc-midturn-ping.py   → posts in-progress text chunks after each tool call
```

### Components

- `bridge.py` — Main relay process. Polls Discord, forwards messages to tmux via SSH. Runs on relay host as a systemd service.
- `cc_hook_utils.py` — Shared utilities imported by all hook scripts. Single source of truth for redaction patterns, token loading, thread resolution, and Discord message splitting.
- `cc-stophook-post.py` — Claude Code `Stop` hook. Runs on the tmux host at the end of each CC turn. Reads transcript, redacts, posts reply to Discord.
- `cc-midturn-ping.py` — Claude Code `PostToolUse` hook. Streams in-progress text to Discord in real time during long tool chains.
- `cc-midturn-post.py` — Background subprocess spawned by `cc-midturn-ping.py` to post text chunks without blocking the hook.
- `_voice_worker.py` — Optional background subprocess. Summarizes reply via local LLM, generates TTS audio (Kokoro/F5), posts MP3 to Discord.
- `cc-pane-watcher.py` — Runs as a LaunchAgent/service on the tmux host. Polls pane content, posts alerts when Claude Code is waiting for approval.
- `cc-bridge-alert` — CLI utility to post an ad-hoc alert to the primary Discord thread.
- `cc-bridge-ssh-guard` — Forced-command script for the bridge SSH key. Whitelists exactly the tmux commands the bridge needs; rejects everything else.
- `cc-discord-bridge.service` — systemd unit for the relay host.

---

## Security model

- **Owner gate**: only messages from `OWNER_ID` (your Discord user snowflake) are forwarded to tmux. All other authors are silently dropped.
- **Claude guard**: before forwarding, the bridge checks that the target pane's foreground process contains "claude". If the CC session has exited and the pane is a bare shell, forwarding is refused — your Discord text would otherwise execute as shell commands.
- **SSH guard**: `cc-bridge-ssh-guard` deployed as a forced command on the tmux host allows only the exact `tmux` subcommands the bridge needs. No arbitrary command execution.
- **Redaction**: any pane output posted to Discord (e.g. `!screen`) is run through `redact()` which strips keys, tokens, private key blocks, and high-entropy strings.
- **No shell interpolation**: all SSH commands are built as argv lists. Content is passed via stdin (`tmux load-buffer -`), never interpolated.

---

## Setup

### 1. Prerequisites

- A Discord bot token (create at https://discord.com/developers/applications)
- A relay host (Linux VPS or always-on machine) where `bridge.py` will run
- A local machine running tmux with Claude Code sessions
- SSH key-based access from relay → local machine (see SSH setup below)

### 2. Configure

Copy `config.example.py` to `config.py` on the relay host and fill in your values:

```python
# config.py
GUILD_ID      = "123456789012345678"   # your Discord server ID
OWNER_ID      = "987654321098765432"   # your Discord user ID
BRIDGE_BOT_ID = "111111111111111111"   # bot's own Discord user ID

CHANNELS = [
    {"name": "CCode",    "thread_id": "THREAD_ID_1", "tmux_target": "cc-main:CCode"},
    # add more as needed
]
CCODE_THREAD_ID = CHANNELS[0]["thread_id"]

TMUX_SESSION = "cc-main"
SSH_TARGET   = "youruser@your-local-ip"
SSH_KEY_PATH = "~/.ssh/cc-bridge_ed25519"
TOKEN_ENV_FILE = "/home/youruser/cc-discord-bridge/.token-env"
```

Create the token file at `TOKEN_ENV_FILE`:
```
DISCORD_BOT_TOKEN_CC=your_bot_token_here
```

### 3. Set up SSH

On the relay host, generate a dedicated key (do not reuse existing keys):
```bash
ssh-keygen -t ed25519 -C "cc-bridge" -f ~/.ssh/cc-bridge_ed25519 -N ""
```

On the local tmux machine, add the public key to `~/.ssh/authorized_keys` with the ssh-guard as a forced command:
```
command="/path/to/cc-bridge-ssh-guard",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA...
```

Edit `cc-bridge-ssh-guard` first to match your session and window names, then install it:
```bash
chmod +x cc-bridge-ssh-guard
# place it somewhere accessible by the SSH daemon
```

Test the connection:
```bash
ssh -i ~/.ssh/cc-bridge_ed25519 -o BatchMode=yes youruser@your-local-ip true && echo OK
```

### 4. Deploy bridge.py (relay host)

```bash
# Clone the repo
git clone https://github.com/pillers/claude-discord-bridge
cd claude-discord-bridge

# Install dependency
pip install requests

# Copy and fill in config
cp config.example.py config.py
$EDITOR config.py

# Create state dir
mkdir -p ~/.cc-bridge

# Install systemd service (edit User/WorkingDirectory first)
sudo cp cc-discord-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cc-discord-bridge
sudo systemctl start cc-discord-bridge
sudo systemctl status cc-discord-bridge

# Tail logs
journalctl -u cc-discord-bridge -f
```

### 5. Deploy hooks (local tmux machine)

The hook scripts run on your local machine (where tmux and Claude Code live).

Set up the token file at `~/.config/cc-bridge-token`:
```
DISCORD_BOT_TOKEN_CCODE=your_bot_token_here
```

Set up thread mapping files (one per tmux window):
```bash
mkdir -p ~/.config/cc-bridge-threads
echo "YOUR_CCODE_THREAD_ID"    > ~/.config/cc-bridge-threads/CCode
echo "YOUR_INFRA_THREAD_ID"    > ~/.config/cc-bridge-threads/CC-Infra
# etc.
```

Or set `CC_BRIDGE_THREAD` in your Claude Code session environment to skip the file lookup.

Configure Claude Code hooks in your `~/.claude/settings.json`:
```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/cc-stophook-post.py"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/cc-midturn-ping.py"
          }
        ]
      }
    ]
  }
}
```

### 6. Deploy pane watcher (local tmux machine)

`cc-pane-watcher.py` monitors all tmux windows and alerts Discord when Claude Code is waiting for approval input.

Install as a macOS LaunchAgent (`~/Library/LaunchAgents/com.local.cc-pane-watcher.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.local.cc-pane-watcher</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/cc-pane-watcher.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/youruser/logs/cc-pane-watcher.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/youruser/logs/cc-pane-watcher.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.local.cc-pane-watcher.plist
```

For Linux, deploy as a systemd user service instead.

---

## Token source

The bridge on the relay host reads `DISCORD_BOT_TOKEN_CC` from the file at `TOKEN_ENV_FILE` (configured in `config.py`).

The hook scripts on the local machine read `DISCORD_BOT_TOKEN_CCODE` from `~/.config/cc-bridge-token`.

The two variable names differ by design — both can hold the same token. The token is **never** written to logs, argv, or Discord messages.

---

## Control commands

Send these from your Discord account in any bridged thread. Commands from any other author are silently dropped.

- `!lock` — disable tmux forwarding for this channel (persisted)
- `!unlock` — re-enable forwarding
- `!status` — post bridge health: lock state, last-input age, ssh/tmux reachability, all channel states
- `!screen` — capture last 40 lines of this channel's tmux pane, redact, post
- `!sessions` — list tmux windows in the configured session
- `!restart` — post notice, exit 0 (systemd revives)
- `!revive` — check/restart the Claude Code process in this channel's pane
- `!mention-only` — toggle: only forward messages that @-mention the bridge bot
- `!!text` — forward literally (strips one leading `!` to allow sending `!commands` to Claude)

---

## Rate limiting

Forwarded messages are token-bucket limited per channel: **1/sec sustained, burst 5**.
Excess messages receive a ⏳ reaction and are not forwarded.

---

## Heartbeat

Posts to the primary thread every 24 hours:
```
bridge alive — forwarding ok, ssh ok, tmux ok, last input 3m ago
```

---

## Voice (optional)

If `~/.config/cc-bridge-voice` exists on the tmux host, `cc-stophook-post.py` will spawn `_voice_worker.py` after each reply.

`_voice_worker.py` requires:
- A local LLM server compatible with OpenAI chat completions API (e.g. llama.cpp, MLX)
  - Configure `LLM_URL` and `LLM_MODEL` in `config.py`
- A TTS server at `http://127.0.0.1:8085/v1/audio/speech` (Kokoro or compatible)

The worker condenses the reply to spoken prose and posts an MP3 attachment to Discord.

---

## Redaction

All pane output posted to Discord (`!screen`, voice summarization input) passes through `redact()`:
- SSH private key blocks
- `Authorization: Bearer/Bot` headers
- `*_API_KEY=`, `*TOKEN=`, `*SECRET=`, `*PASSWORD=` assignments
- AWS-style `AKIA…` key IDs
- GitHub tokens (`ghp_`, `gho_`, etc.)
- Slack tokens (`xoxb-`, etc.)
- Discord bot token shapes
- High-entropy standalone strings > 40 chars

The `_REDACT_PATTERNS` in `bridge.py` and `_REDACT` in `cc_hook_utils.py` are intentionally maintained separately to allow independent tuning, though they cover the same categories.
