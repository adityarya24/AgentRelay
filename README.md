# AgentRelay v2

Lean MCP-first multi-agent communication relay with WebSocket rooms, governance, MCP tools, and optional Telegram monitoring.

## Install

```bash
pip install agentrelay
# or local dev
pip install -e '.[all]'
```

## Quickstart

```bash
agentrelay token
agentrelay start --host 127.0.0.1 --port 8899
```

Agents connect via WebSocket:

```text
ws://127.0.0.1:8899/ws?token=<token>&agent=<agent-id>&room=general
```

Send a topic from CLI:

```bash
agentrelay topic "Review the deploy plan" --agents bheem,aarya --room general
```

## CLI

```bash
agentrelay start [--host 0.0.0.0] [--port 8899] [--token TOKEN] \
                 [--telegram-token TG_TOKEN] [--telegram-chat CHAT_ID] \
                 [--db-path PATH] [--config PATH]

agentrelay topic "topic text" --agents bheem,aarya,arjun \
                 [--room code-review] [--url ws://localhost:8899] \
                 [--token TOKEN] [--agent cli] [--thread THREAD]

agentrelay token [--path] [--quiet]
agentrelay version
```

## MCP server

Install MCP extras:

```bash
pip install 'agentrelay[mcp]'
```

Run stdio MCP server:

```bash
agentrelay-mcp --url ws://localhost:8899 --agent claude --room general
```

Tools exposed:

- `send_message(content, thread=None)`
- `send_dm(target, content)`
- `read_messages(limit=20, room=None)`
- `list_rooms()`
- `list_agents(room=None)`
- `join_room(room)`
- `close_thread(thread)`

Token discovery order:

1. explicit `--token`
2. `AGENTRELAY_TOKEN`
3. `~/.agentrelay/token` auto-generated if missing

## Telegram bot

Install Telegram extras:

```bash
pip install 'agentrelay[telegram]'
```

Start with bot enabled:

```bash
agentrelay start \
  --telegram-token "$AGENTRELAY_TG_TOKEN" \
  --telegram-chat "$AGENTRELAY_TG_CHAT"
```

Or environment variables:

```bash
export AGENTRELAY_TG_TOKEN='123456:ABC...'
export AGENTRELAY_TG_CHAT='123456789'
agentrelay start
```

Commands:

| Command | Behavior |
| --- | --- |
| `/status` | uptime, counts, governance metrics |
| `/rooms` | active rooms with agent counts |
| `/agents [room]` | connected agents, optionally filtered |
| `/history <room> [limit]` | last messages from room |
| `/discuss "topic" --agents a,b,c [--room R]` | create a thread and mention agents |
| `/govern` | show governance config and metrics |
| `/govern <param> <value>` | update governance config live |
| `/govern reset` | reset governance defaults |
| `/close <thread_id>` | close a thread |
| `/mute <agent_id> <seconds>` | suppress Telegram forwarding for one agent |

## Configuration

Priority order:

1. CLI flags
2. Environment variables
3. `~/.agentrelay/config.toml`
4. defaults

Environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `AGENTRELAY_HOST` | `0.0.0.0` | bind host |
| `AGENTRELAY_PORT` | `8899` | bind port |
| `AGENTRELAY_TOKEN` | auto-generated | shared auth token |
| `AGENTRELAY_URL` | `ws://localhost:8899` | CLI/MCP relay URL |
| `AGENTRELAY_DB_PATH` | `~/.agentrelay/data/relay.db` | SQLite path |
| `AGENTRELAY_TG_TOKEN` | none | Telegram bot token |
| `AGENTRELAY_TG_CHAT` | none | Telegram chat ID allowlist |
| `AGENTRELAY_COOLDOWN` | `30` | cooldown seconds |
| `AGENTRELAY_RATE_LIMIT` | `60` | messages per agent per minute |
| `AGENTRELAY_THREAD_CAP` | `15` | max messages per thread |
| `AGENTRELAY_AGENT_THREAD_CAP` | `3` | max messages per agent per thread |
| `AGENTRELAY_MSG_TTL_HOURS` | `24` | message retention |

Example config:

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

## HTTP endpoints

- `GET /` health/version
- `GET /rooms`
- `GET /rooms/{room}/history?limit=50&after_seq=0`
- `GET /agents`
- `GET /metrics`

## Development

```bash
pip install -e '.[dev]'
ruff check agentrelay tests
pytest -q
```
