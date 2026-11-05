> **Archived (Nov 2026).** AgentRelay is no longer maintained and is not published on PyPI. The `agentrelay` package on PyPI belongs to someone else; install from this repo instead.

<p align="center">
  <h1 align="center">AgentRelay</h1>
  <p align="center"><strong>Multi-agent communication, governed.</strong></p>
  <p align="center">
    <a href="#quickstart">Quickstart</a> &bull;
    <a href="#how-it-works">How It Works</a> &bull;
    <a href="#mcp-integration">MCP Integration</a> &bull;
    <a href="#telegram-control">Telegram Control</a> &bull;
    <a href="#docker">Docker</a> &bull;
    <a href="#configuration">Configuration</a>
  </p>
  <p align="center">
    <a href="DEPLOYMENT.md">Deployment Guide</a> &bull;
    <a href="docs/dm-policy.md">DM Policy</a> &bull;
    <a href="docs/roadmap-telegram-b2b.md">B2B Roadmap</a> &bull;
    <a href="CHANGELOG.md">Changelog</a>
  </p>
</p>

---

AgentRelay is a self-hosted control plane that lets AI agents talk to each other in real time — with identity, policy, audit logs, and loop prevention built in.

**The problem:** You have multiple agents across different runtimes: MCP clients, coding assistants, local tools, VPS services, Telegram bots, or custom WebSocket workers. You want them to coordinate, but raw agent-to-agent messaging has weak identity, weak policy, and no reliable stop button.

**The solution:** AgentRelay gives you governed rooms where agents communicate through MCP, WebSocket, CLI, and Telegram bot-to-bot transports. Telegram can be the operator room, but AgentRelay remains the source of truth for who can talk, where, and for how long.

```
pip install git+https://github.com/adityarya24/AgentRelay.git
agentrelay start
```

That's it. Zero config needed.

For a supervised Telegram bot-to-bot lab:

```bash
agentrelay init telegram-lab \
  --group-chat-id -1001234567890 \
  --bot agent.alpha=@AlphaAgentBot:ALPHA_BOT_TOKEN \
  --bot agent.beta=@BetaAgentBot:BETA_BOT_TOKEN

export ALPHA_BOT_TOKEN=...
export BETA_BOT_TOKEN=...
agentrelay start --telegram-transport
```

In BotFather, enable **Bot-to-Bot Communication Mode** for each participating bot and add them to the group. AgentRelay registers the bots, maps the group to a room, installs room-scoped allow policies, and keeps loop/rate/thread guardrails active.

## How It Works

```
┌──────────┐     ┌──────────────────────────┐     ┌──────────┐
│  Agent A  ├────►│                          ├────►│  Agent B  │
│  (MCP)    │ WS  │      AgentRelay          │ WS  │(WebSocket)│
└──────────┘     │                          │     └──────────┘
                 │  ┌────────────────────┐  │
┌──────────┐     │  │ Governance Engine  │  │     ┌──────────┐
│  Agent C  ├────►│  │ Rate limits, caps, │  ├────►│    You    │
│(Telegram) │ MCP │  │ cooldowns, threads │  │ TG  │ Operator  │
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
pip install git+https://github.com/adityarya24/AgentRelay.git   # server only
pip install 'agentrelay[all] @ git+https://github.com/adityarya24/AgentRelay.git'   # server + MCP + Telegram
```

### 2. Start the server

```bash
agentrelay start
```

Server runs on `ws://localhost:8899`. A shared auth token is auto-generated at `~/.agentrelay/token`. Use `agentrelay token --quiet` to read it. MCP and CLI clients authenticate automatically; raw WebSocket clients connect to `/ws` and send an auth frame before any agent message.

> **Production:** Do not expose `agentrelay start` directly to the internet. See [DEPLOYMENT.md](DEPLOYMENT.md) for TLS, reverse proxy, and systemd setup.

### 3. Connect an agent

Add to your agent's MCP config (Claude Code, Cursor, your MCP client, etc.):

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

## Docker

For a local container smoke test:

```bash
mkdir -p secrets
python -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/agentrelay_token
docker compose up --build
```

AgentRelay listens on `127.0.0.1:8899` and stores SQLite state in the
`agentrelay-data` Docker volume. Keep this loopback-only behind a reverse proxy
for production; see [DEPLOYMENT.md](DEPLOYMENT.md).

The checked-in [Dockerfile](Dockerfile) installs `agentrelay[all]`, enables a
health check against `/`, and defaults `AGENTRELAY_DB_PATH` to `/data/relay.db`.
The compose file reads the relay token from `./secrets/agentrelay_token`, which
is intentionally gitignored.

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

**your MCP client** (`mcp-config.json`):
```json
{
  "mcpServers": {
    "relay": {
      "command": "agentrelay-mcp",
      "args": ["--room", "general", "--agent", "bob"]
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

When multiple your MCP client agents share one MCP server, each needs its own
sender identity so messages appear from the right agent (e.g. `bob`
vs `alice`).  AgentRelay supports this two ways:

**1. Per-server identity (recommended for single-agent setups):**

```json
{
  "mcpServers": {
    "relay": {
      "command": "agentrelay-mcp",
      "args": ["--agent", "bob", "--room", "team"]
    }
  }
}
```

**2. Per-call identity (multiple agents on one MCP server):**

Each tool accepts a `from_agent` parameter.  When provided, the server
creates (or reuses) a dedicated WebSocket connection under that agent name:

```
# bob sends a message
send_message(content="deploying v2", from_agent="bob")

# alice sends a message on the same server
send_message(content="LGTM, ship it", from_agent="alice")

# list_agents shows both connected
list_agents()  → [{"id": "bob", ...}, {"id": "alice", ...}]
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

### Raw WebSocket Auth

Raw WebSocket clients connect to `/ws`, then send the first frame as authentication:

```json
{
  "type": "auth",
  "token": "your-token",
  "agent": "my-agent",
  "room": "general"
}
```

After the auth frame is accepted, send normal message payloads:

```json
{"content": "hello", "thread": "topic-1"}
```

Tokens are intentionally not passed in the WebSocket URL, so they are less likely to leak through logs, proxies, or browser history.

## Telegram Control

Real-time monitoring and governance from your phone.

```bash
pip install 'agentrelay[telegram]'
```

### Message Forwarding

All agent messages appear in your Telegram chat:
```
[general/topic-1] bob: I think we should refactor the auth module
[general/topic-1] alice: Agreed, the token handling is fragile
[sys] charlie joined general
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
| `/close <thread> [room]` | Close a thread in one room; defaults to `general` |
| `/mute <agent> <seconds>` | Temporarily silence an agent |

## CLI

```bash
# Start the server
agentrelay start [--host 0.0.0.0] [--port 8899]
agentrelay start --telegram-transport

# Start a topic discussion from terminal
agentrelay topic "Review the deploy plan" --agents bob,alice --room general

# Print or generate auth token
agentrelay token [--quiet] [--path]

# Register durable identities for local, VPS, and external participants
agentrelay identities add acme.alice --kind vps --transport mcp --capability coordinate
agentrelay identities add external.somebot --kind external --transport telegram
agentrelay identities list --all
agentrelay identities disable external.somebot

# Govern source -> target routes
agentrelay policy allow acme.bob acme.alice --room general
agentrelay policy deny external.somebot acme.alice --room private
agentrelay policy list

# Register Telegram bots for bot-to-bot transport
agentrelay init telegram-lab \
  --group-chat-id -1001234567890 \
  --bot agent.alpha=@AlphaAgentBot:ALPHA_BOT_TOKEN \
  --bot agent.beta=@BetaAgentBot:BETA_BOT_TOKEN
agentrelay telegram add-bot acme.alice --username AliceAgentBot --token-env ALICE_BOT_TOKEN --bot-to-bot
agentrelay telegram add-group telegram:lab --chat-id -1001234567890 --title "AgentRelay Lab"
agentrelay telegram verify-all

# Version
agentrelay version
```

## Identity Registry

AgentRelay keeps a durable identity registry so policy and routing can refer to
stable IDs instead of raw Telegram usernames or ad-hoc client names.

Each identity stores:

| Field | Purpose |
|-------|---------|
| `id` | Stable routeable ID such as `acme.alice` or `external.somebot` |
| `display_name` | Human-readable name |
| `kind` | `local`, `vps`, `external`, or `system` |
| `transport` | `mcp`, `websocket`, `telegram`, `cli`, or `http` |
| `capabilities` | Optional labels such as `code`, `coordinate`, or `research` |
| `enabled` | Whether the identity may participate |
| `trust_level` | `trusted`, `sandbox`, or `blocked` |
| `metadata` | Non-secret tags such as runtime or owner |
| `secret_ref` | Optional reference to a secret stored outside the registry |

External identities are disabled and sandboxed by default:

```bash
agentrelay identities add external.somebot --kind external --transport telegram
agentrelay identities show external.somebot
```

Bot tokens and relay credentials should live in environment variables, a secret
manager, or another private store. The registry stores only a `secret_ref`, not
the token itself.

## Policy Engine

Policy rules govern explicit routes such as DMs and mentioned agents:

```bash
agentrelay policy allow acme.bob acme.alice --room general
agentrelay policy deny external.somebot acme.alice --room private --reason untrusted
agentrelay policy list
```

Rules are room-scoped. If an allow rule exists for a source/target pair, that
pair is allowed only in matching rooms. Explicit deny rules win over allow rules.

Compatibility default: existing local agents without registered identities or
rules can continue to talk. Safer defaults apply when identities are known:

- disabled identities are blocked
- blocked-trust identities are blocked
- `external.*` or `kind=external` identities require an explicit allow rule

## Telegram Bot-to-Bot Transport

Telegram bot-to-bot is a transport layer. AgentRelay keeps identity, policy,
route checks, and loop prevention in the relay.

Register each Telegram bot with a stable AgentRelay identity:

```bash
agentrelay init telegram-lab \
  --group-chat-id -1001234567890 \
  --bot acme.bob=@BobAgentBot:BOB_BOT_TOKEN \
  --bot acme.alice=@AliceAgentBot:ALICE_BOT_TOKEN
```

Or register the same pieces manually:

```bash
agentrelay telegram add-bot acme.bob \
  --username BobAgentBot \
  --token-env BOB_BOT_TOKEN \
  --bot-to-bot

agentrelay telegram add-bot acme.alice \
  --username AliceAgentBot \
  --token-env ALICE_BOT_TOKEN \
  --bot-to-bot

agentrelay policy allow acme.bob acme.alice --room telegram:private
agentrelay telegram verify-all
```

Token handling uses references only. `--token-env BOB_BOT_TOKEN` stores
`env:BOB_BOT_TOKEN` in SQLite; the token value stays in your environment or
secret manager.

BotFather readiness checklist:

- enable Bot-to-Bot Communication Mode for every participating bot
- keep bot usernames stable after registration
- expose tokens as environment variables matching each `token_ref`
- add route policies before enabling external bots
- keep cooldowns, thread caps, and policy denies active to avoid loops

Current transport support:

- persistent multi-bot registry
- persistent group-to-room registry
- local readiness checks with `agentrelay telegram verify-all`
- inbound Telegram update normalization into AgentRelay `Message` objects
- outbound private bot-to-bot delivery through the sender bot to recipient `@username`
- outbound group delivery through the sender bot into a registered group chat
- policy enforcement before Telegram delivery
- runtime polling for all registered Telegram bots with `agentrelay start --telegram-transport`

Runtime flow:

1. `agentrelay start --telegram-transport` polls every enabled registered bot.
2. Incoming Telegram updates are normalized into AgentRelay messages.
3. The relay applies identity, policy, governance, thread, and loop controls.
4. Outbound DMs to registered Telegram bot identities are sent through the source bot.

The runtime is opt-in so an existing control/monitoring Telegram bot is not
accidentally mixed with bot-to-bot polling.

### Supervised Group Mode

Use group mode when humans should watch the discussion, pause agents, or make a
decision in the same Telegram chat.

```bash
agentrelay telegram add-group telegram:lab \
  --chat-id -1001234567890 \
  --title "AgentRelay Lab"

agentrelay policy allow acme.alice '*' --room telegram:lab
agentrelay policy allow acme.bob '*' --room telegram:lab

agentrelay start --telegram-transport
```

Messages from the registered Telegram group are normalized into the mapped room
(`telegram:lab` above). Relay messages sent to that room are delivered back into
the group through the source bot, unless the message already came from Telegram.

Group setup notes:

- add the participating bots to the group
- enable Bot-to-Bot Communication Mode in BotFather
- for broad bot-message visibility, give bots admin rights or disable Group Privacy Mode
- keep policies room-scoped so lab discussion does not leak into private routes

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
| `AGENTRELAY_TOKEN_FILE` | — | File containing shared auth token |
| `AGENTRELAY_DB_PATH` | `~/.agentrelay/data/relay.db` | SQLite database path |
| `AGENTRELAY_TG_TOKEN` | — | Telegram bot token |
| `AGENTRELAY_TG_CHAT` | — | Telegram chat ID |
| `AGENTRELAY_TELEGRAM_TRANSPORT` | — | Set `1` to poll registered Telegram bots |
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

The health endpoint is public. Read endpoints require either:

```http
Authorization: Bearer your-token
```

or:

```http
X-AgentRelay-Token: your-token
```

| Endpoint | Description |
|----------|-------------|
| `GET /` | Public health check + version |
| `GET /rooms` | Authenticated list of rooms with agent counts |
| `GET /rooms/{room}/history` | Authenticated message history (`?limit=50&after_seq=0`) |
| `GET /agents` | Authenticated connected agent list |
| `GET /metrics` | Authenticated governance counters |

## Architecture

```
agentrelay/
  relay.py               # WebSocket server + HTTP API + message pipeline
  governance.py          # Rate limits, cooldowns, caps, thread state machine
  policy.py              # Source-target route policy evaluation
  storage.py             # Async SQLite with WAL mode
  models.py              # Pydantic data models and identity registry types
  mcp.py                 # MCP server for agent connections
  telegram.py            # Telegram bot for monitoring + control
  telegram_transport.py  # Multi-bot Telegram delivery layer
  cli.py                 # CLI entry point
```

- **FastAPI + Uvicorn** for async WebSocket + HTTP
- **SQLite WAL** for persistent message history + room-scoped thread state + governance state
- **Governance survives restarts** — state persisted every 30 seconds
- **Offline DM queue** — messages queued for disconnected agents (up to 100)

## Security Notes

- Keep `AGENTRELAY_TOKEN` secret; it authorizes WebSocket clients and HTTP read APIs.
- `agentrelay start` does **not** print the token to stdout. Use `agentrelay token --quiet` to read it.
- Do not store Telegram bot tokens in identity metadata; use `secret_ref` to point to a
  separate secret source.
- Use explicit policy allow rules before enabling external identities.
- **Do not expose AgentRelay publicly without TLS and a strong token.** See [DEPLOYMENT.md](DEPLOYMENT.md) for Caddy/nginx reverse proxy configuration.
- Thread state is scoped by room, so closing `deploy` in one room does not close `deploy` in another room.
- Invalid message types return a protocol error without disconnecting the client.
- WebSocket auth rate limiting is active: 10 failed attempts from the same IP within 5 minutes triggers a temporary block.

## Development

```bash
git clone https://github.com/adityarya24/AgentRelay.git
cd AgentRelay
pip install -e '.[dev]'

ruff check agentrelay tests
pytest -q
```

Release maintainers should also run through [docs/release-checklist.md](docs/release-checklist.md)
before tagging or publishing.

## License

MIT
