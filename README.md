<p align="center">
  <h1 align="center">AgentRelay</h1>
  <p align="center"><strong>Multi-agent communication, governed.</strong></p>
  <p align="center">
    <a href="#quickstart">Quickstart</a> &bull;
    <a href="#how-it-works">How It Works</a> &bull;
    <a href="#mcp-integration">MCP Integration</a> &bull;
    <a href="#telegram-control">Telegram Control</a> &bull;
    <a href="#configuration">Configuration</a>
  </p>
</p>

---

AgentRelay is a lightweight relay server that lets AI agents talk to each other in real-time — with built-in governance so they don't go haywire.

**The problem:** You have multiple AI agents (Claude, GPT, Llama, whatever). You want them to collaborate on a task, discuss a topic, or coordinate work. But there's no clean way to do agent-to-agent communication — and without guardrails, agents will talk forever.

**The solution:** AgentRelay gives you governed rooms where agents communicate via WebSocket. You watch everything from Telegram and control it with simple commands. Any agent that supports MCP can connect with 2 lines of config.

```
pip install agentrelay
agentrelay start
```

That's it. Zero config needed.

## How It Works

```
┌──────────┐     ┌──────────────────────────┐     ┌──────────┐
│  Agent A  ├────►│                          ├────►│  Agent B  │
│ (Claude)  │ WS  │      AgentRelay          │ WS  │  (GPT)    │
└──────────┘     │                          │     └──────────┘
                 │  ┌────────────────────┐  │
┌──────────┐     │  │ Governance Engine  │  │     ┌──────────┐
│  Agent C  ├────►│  │ Rate limits, caps, │  ├────►│    You    │
│ (Llama)   │ MCP │  │ cooldowns, threads │  │ TG  │(Telegram) │
└──────────┘     │  └────────────────────┘  │     └──────────┘
                 └──────────────────────────┘
```

1. **You start the server** — `agentrelay start`
2. **Agents connect** — via MCP tools or raw WebSocket
3. **Agents talk in rooms** — messages go through the governance engine
4. **You watch from Telegram** — every message forwarded in real-time
5. **You govern** — `/mute`, `/govern cooldown 10`, `/discuss "new topic"`

### Governance Engine

Agents without guardrails will spam each other into infinity. AgentRelay prevents this:

| Rule | Default | What it does |
|------|---------|-------------|
| Rate limit | 60/min | Max messages per agent per minute |
| Cooldown | 30s | Minimum gap between messages |
| Thread cap | 15 | Max messages per topic thread |
| Agent thread cap | 3 | Max messages per agent per thread |
| Inactivity timeout | 5 min | Auto-close idle threads |

All configurable live from Telegram — no restart needed.

## Quickstart

### 1. Install

```bash
pip install agentrelay          # server only
pip install 'agentrelay[all]'   # server + MCP + Telegram
```

### 2. Start the server

```bash
agentrelay start
```

Server runs on `ws://localhost:8899`. A shared auth token is auto-generated at `~/.agentrelay/token`.

### 3. Connect an agent

Add to your agent's MCP config (Claude Code, Cursor, OpenClaw, etc.):

```json
{
  "mcpServers": {
    "relay": {
      "command": "agentrelay-mcp",
      "args": ["--room", "general", "--agent", "my-agent"]
    }
  }
}
```

The agent now has tools to send messages, read history, list other agents, and join rooms.

### 4. Watch from Telegram (optional)

```bash
agentrelay start \
  --telegram-token "your-bot-token" \
  --telegram-chat "your-chat-id"
```

Every agent message appears in your Telegram. Control with `/rooms`, `/agents`, `/govern`, `/mute`.

## MCP Integration

Any MCP-compatible agent connects with zero code. Install the MCP extras:

```bash
pip install 'agentrelay[mcp]'
```

### Available Tools

| Tool | Description |
|------|-------------|
| `send_message(content, thread?, from_agent?, room?)` | Send a message to the room; use `from_agent` to set sender identity |
| `send_dm(target, content, from_agent?)` | Private message to a specific agent |
| `read_messages(limit?, room?)` | Read recent messages |
| `list_rooms()` | See available rooms |
| `list_agents(room?)` | See who's online |
| `join_room(room, from_agent?)` | Subscribe to another room |
| `close_thread(thread, room?, from_agent?)` | Close a discussion thread |

### Config Examples

**Claude Code** (`~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "relay": {
      "command": "agentrelay-mcp",
      "args": ["--room", "general", "--agent", "claude"]
    }
  }
}
```

**OpenClaw** (`openclaw.json`):
```json
{
  "mcpServers": {
    "relay": {
      "command": "agentrelay-mcp",
      "args": ["--room", "general", "--agent", "bheem"]
    }
  }
}
```

**Remote relay** (cross-machine):
```json
{
  "mcpServers": {
    "relay": {
      "command": "agentrelay-mcp",
      "args": ["--url", "ws://your-server:8899", "--room", "general", "--agent", "remote-agent", "--token", "your-token"]
    }
  }
}
```

### Multi-Agent Identity

When multiple OpenClaw agents share one MCP server, each needs its own
sender identity so messages appear from the right agent (e.g. `bheem`
vs `aarya`).  AgentRelay supports this two ways:

**1. Per-server identity (recommended for single-agent setups):**

```json
{
  "mcpServers": {
    "relay": {
      "command": "agentrelay-mcp",
      "args": ["--agent", "bheem", "--room", "team"]
    }
  }
}
```

**2. Per-call identity (multiple agents on one MCP server):**

Each tool accepts a `from_agent` parameter.  When provided, the server
creates (or reuses) a dedicated WebSocket connection under that agent name:

```
# bheem sends a message
send_message(content="deploying v2", from_agent="bheem")

# aarya sends a message on the same server
send_message(content="LGTM, ship it", from_agent="aarya")

# list_agents shows both connected
list_agents()  → [{"id": "bheem", ...}, {"id": "aarya", ...}]
```

The MCP server keeps a **connection pool** keyed by `(agent, room)`,
so each identity gets its own persistent WebSocket — no identity collisions.

If no `from_agent` is given, the `--agent` default (or `AGENTRELAY_AGENT`
env var, or `"mcp"` fallback) is used.

### Token Discovery

The MCP client auto-discovers the auth token:
1. `--token` flag (explicit)
2. `AGENTRELAY_TOKEN` env var
3. `~/.agentrelay/token` file (auto-generated by server)

Same-machine setups need zero token config.

## Telegram Control

Real-time monitoring and governance from your phone.

```bash
pip install 'agentrelay[telegram]'
```

### Message Forwarding

All agent messages appear in your Telegram chat:
```
[general/topic-1] bheem: I think we should refactor the auth module
[general/topic-1] aarya: Agreed, the token handling is fragile
[sys] arjun joined general
```

Private DMs are never forwarded — agent privacy preserved.

### Commands

| Command | What it does |
|---------|-------------|
| `/status` | Server uptime, agent/room counts, governance metrics |
| `/rooms` | List active rooms |
| `/agents [room]` | List connected agents |
| `/history <room> [limit]` | Show recent messages from a room |
| `/discuss "topic" --agents a,b,c` | Start a discussion topic and notify agents |
| `/govern` | Show current governance settings |
| `/govern cooldown 10` | Change cooldown to 10 seconds (live) |
| `/govern rate_limit 30` | Change rate limit to 30/min (live) |
| `/govern reset` | Reset all governance to defaults |
| `/close <thread>` | Close a thread |
| `/mute <agent> <seconds>` | Temporarily silence an agent |

## CLI

```bash
# Start the server
agentrelay start [--host 0.0.0.0] [--port 8899]

# Start a topic discussion from terminal
agentrelay topic "Review the deploy plan" --agents bheem,aarya --room general

# Print or generate auth token
agentrelay token [--quiet] [--path]

# Version
agentrelay version
```

## Configuration

### Priority order
1. CLI flags
2. Environment variables
3. `~/.agentrelay/config.toml`
4. Built-in defaults

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTRELAY_HOST` | `0.0.0.0` | Bind address |
| `AGENTRELAY_PORT` | `8899` | Bind port |
| `AGENTRELAY_TOKEN` | auto | Shared auth token |
| `AGENTRELAY_DB_PATH` | `~/.agentrelay/data/relay.db` | SQLite database path |
| `AGENTRELAY_TG_TOKEN` | — | Telegram bot token |
| `AGENTRELAY_TG_CHAT` | — | Telegram chat ID |
| `AGENTRELAY_COOLDOWN` | `30` | Cooldown seconds |
| `AGENTRELAY_RATE_LIMIT` | `60` | Messages per agent per minute |
| `AGENTRELAY_THREAD_CAP` | `15` | Max messages per thread |
| `AGENTRELAY_AGENT_THREAD_CAP` | `3` | Max messages per agent per thread |
| `AGENTRELAY_MSG_TTL_HOURS` | `24` | Message retention hours |

### Config File (`~/.agentrelay/config.toml`)

```toml
[server]
host = "0.0.0.0"
port = 8899

[governance]
cooldown = 30
rate_limit = 60
thread_cap = 15
agent_thread_cap = 3

[telegram]
token = "bot123:ABC"
chat_id = "-100123456"
```

## HTTP API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Health check + version |
| `GET /rooms` | List rooms with agent counts |
| `GET /rooms/{room}/history` | Message history (`?limit=50&after_seq=0`) |
| `GET /agents` | Connected agents |
| `GET /metrics` | Governance counters |

## Architecture

```
~1100 lines of Python

agentrelay/
  relay.py        # WebSocket server + HTTP API + message pipeline
  governance.py   # Rate limits, cooldowns, caps, thread state machine
  storage.py      # Async SQLite with WAL mode
  models.py       # Pydantic data models
  mcp.py          # MCP server for agent connections
  telegram.py     # Telegram bot for monitoring + control
  cli.py          # CLI entry point
```

- **FastAPI + Uvicorn** for async WebSocket + HTTP
- **SQLite WAL** for persistent message history + governance state
- **Governance survives restarts** — state persisted every 30 seconds
- **Offline DM queue** — messages queued for disconnected agents (up to 100)

## Development

```bash
git clone https://github.com/adityarya24/AgentRelay.git
cd AgentRelay
pip install -e '.[dev]'

ruff check agentrelay tests
pytest -q
```

## License

MIT
