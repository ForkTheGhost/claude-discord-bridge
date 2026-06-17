"""
cc-discord-bridge configuration.

Copy this file to config.py and fill in your values.
config.py is gitignored — never commit it.
"""

# ---------------------------------------------------------------------------
# Discord IDs
# ---------------------------------------------------------------------------

# Your Discord server (guild) ID
GUILD_ID = "YOUR_GUILD_ID"

# Your personal Discord user ID — ONLY messages from this user are ever forwarded.
# This is the sole security gate for tmux forwarding. Do not leave it blank.
OWNER_ID = "YOUR_DISCORD_USER_ID"

# The Discord bot's own user ID (used for mention-only mode filtering)
BRIDGE_BOT_ID = "YOUR_BOT_USER_ID"

# ---------------------------------------------------------------------------
# Discord thread/channel IDs
# One entry per Claude Code tmux window you want to bridge.
# thread_id: Discord thread or channel snowflake ID
# tmux_target: "session:window" as tmux understands it
# ---------------------------------------------------------------------------

CHANNELS = [
    {"name": "CCode",    "thread_id": "YOUR_CCODE_THREAD_ID",    "tmux_target": "cc-main:CCode"},
    {"name": "CC-Infra", "thread_id": "YOUR_INFRA_THREAD_ID",    "tmux_target": "cc-main:CC-Infra"},
    {"name": "CC-ArdI",  "thread_id": "YOUR_ARDI_THREAD_ID",     "tmux_target": "cc-main:CC-ArdI"},
    {"name": "CC-Palo",  "thread_id": "YOUR_PALO_THREAD_ID",     "tmux_target": "cc-main:CC-Palo"},
]

# The first channel's thread_id — used for heartbeat and startup announcements.
# Set this to the same thread_id as CHANNELS[0].
CCODE_THREAD_ID = CHANNELS[0]["thread_id"]

# ---------------------------------------------------------------------------
# tmux session name on the remote host
# ---------------------------------------------------------------------------

TMUX_SESSION = "cc-main"

# ---------------------------------------------------------------------------
# SSH connection to the machine running tmux (the "target" host)
# bridge.py runs on a relay host (e.g. a VPS); it SSHes into your local machine.
# ---------------------------------------------------------------------------

# SSH target in user@host format
SSH_TARGET = "youruser@your-local-machine-ip"

# Path to the SSH private key on the relay host (used for passwordless auth)
# Generate with: ssh-keygen -t ed25519 -C "cc-bridge" -f ~/.ssh/cc-bridge_ed25519 -N ""
SSH_KEY_PATH = "~/.ssh/cc-bridge_ed25519"

# ---------------------------------------------------------------------------
# Token file (on the relay host)
# bridge.py reads the bot token from this file. The file must contain:
#   export DISCORD_BOT_TOKEN_CC=your_token_here
# or simply:
#   DISCORD_BOT_TOKEN_CC=your_token_here
# ---------------------------------------------------------------------------

TOKEN_ENV_FILE = "/home/youruser/cc-discord-bridge/.token-env"

# ---------------------------------------------------------------------------
# Local LLM for voice worker (optional — only used by _voice_worker.py)
# Set to None to disable the LLM summarization step (TTS will receive raw text).
# ---------------------------------------------------------------------------

# llama.cpp / MLX server endpoint
LLM_URL   = "http://127.0.0.1:8080/v1/chat/completions"

# Model identifier sent to the LLM server (path for MLX, model name for others)
# Example for MLX: "/path/to/your/model"
# Example for llama.cpp: "local-model"
LLM_MODEL = "local-model"
