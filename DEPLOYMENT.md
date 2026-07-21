# AgentRelay Production Deployment Guide

AgentRelay is currently deployed as an OpenClaw-focused VPS sidecar. Use this guide for Aditya's OpenClaw stack first; generic public deployment can be revisited after the OpenClaw workflow is stable.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Network Security](#network-security)
  - [TLS is Required](#tls-is-required)
  - [Reverse Proxy](#reverse-proxy)
  - [Firewall](#firewall)
- [Installation](#installation)
- [Docker Compose](#docker-compose)
- [Environment Variables](#environment-variables)
- [systemd Service](#systemd-service)
- [Log Rotation](#log-rotation)
- [SQLite Notes](#sqlite-notes)
- [Windows Deployment Notes](#windows-deployment-notes)

---

## Prerequisites

- Python 3.11+
- A Linux server (Ubuntu 22.04/24.04 recommended) or container
- A registered domain with DNS pointing to your server (for TLS)
- Root or sudo access

---

## Network Security

### TLS is Required

`agentrelay start` runs plain HTTP on `0.0.0.0:8899`. **Do not expose this port directly to the internet.**

The WebSocket auth token must never travel over plaintext. Always terminate TLS at a reverse proxy in front of AgentRelay.

### Reverse Proxy

**Caddy (recommended)** — handles TLS automatically with Let's Encrypt:

```caddyfile
relay.yourdomain.com {
    reverse_proxy localhost:8899
}
```

Just install Caddy, drop the above into `/etc/caddy/Caddyfile`, and reload. It will obtain and renew a certificate automatically.

**nginx** — if you already run nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name relay.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/relay.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/relay.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8899;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }
}
```

The WebSocket upgrade headers (`Upgrade` and `Connection`) are mandatory for `/ws` to function through a reverse proxy.

### Firewall

Close port `8899` from the public internet. Only the reverse proxy should reach it.

```bash
# UFW (Ubuntu)
ufw default deny incoming
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP -> HTTPS redirect
ufw allow 443/tcp   # HTTPS
ufw allow from 127.0.0.1 to any port 8899  # Relay, loopback only
ufw enable
```

---

## Installation

```bash
# Create a dedicated user
sudo useradd -r -s /bin/false agentrelay

# Install the package
sudo pip install agentrelay-2.1.0-py3-none-any.whl

# Create directories
sudo mkdir -p /var/lib/agentrelay /etc/agentrelay
sudo chown agentrelay:agentrelay /var/lib/agentrelay

# Generate or place your token
python -c "import secrets; print(secrets.token_urlsafe(32))" | sudo tee /etc/agentrelay/token >/dev/null
sudo chmod 600 /etc/agentrelay/token
sudo chown agentrelay:agentrelay /etc/agentrelay/token
```

---

## Docker Compose

The repository includes a production-shaped local compose example:

- [Dockerfile](Dockerfile)
- [docker-compose.yml](docker-compose.yml)

Create a token file and start the service:

```bash
mkdir -p secrets
python -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/agentrelay_token
docker compose up --build -d
```

The compose file binds AgentRelay to `127.0.0.1:8899`, stores SQLite data in a
named Docker volume, and reads the relay token from a Docker secret backed by
`./secrets/agentrelay_token`.

Verify:

```bash
python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8899/', timeout=3)))"
```

For internet-facing use, keep this loopback binding and put Caddy or nginx in
front of it for TLS.

---

## Environment Variables

| Variable | Example | Purpose |
|----------|---------|---------|
| `AGENTRELAY_TOKEN` | `sk-...` | Auth token. Prefer `AGENTRELAY_TOKEN_FILE` in production. |
| `AGENTRELAY_TOKEN_FILE` | `/etc/agentrelay/token` | Path to a file containing only the token. |
| `AGENTRELAY_DB_PATH` | `/var/lib/agentrelay/relay.db` | SQLite database path. |
| `AGENTRELAY_HOST` | `127.0.0.1` | Bind address. Use `127.0.0.1` behind a reverse proxy. |
| `AGENTRELAY_PORT` | `8899` | Bind port. |
| `AGENTRELAY_MSG_TTL_HOURS` | `72` | How long to keep messages before cleanup. |
| `AGENTRELAY_TG_TOKEN` | `123456:ABC...` | Telegram control bot token (optional). |
| `AGENTRELAY_TG_CHAT` | `-100123...` | Telegram chat ID for control (optional). |
| `AGENTRELAY_TELEGRAM_TRANSPORT` | `1` | Enable multi-bot polling transport (optional). |

Set these in the systemd service file (see below) or in `/etc/agentrelay/environment` loaded via `EnvironmentFile=`.

---

## systemd Service

Checked-in samples:

- [deploy/systemd/agentrelay.service](deploy/systemd/agentrelay.service)
- [deploy/systemd/agentrelay.env.example](deploy/systemd/agentrelay.env.example)

Install them:

```bash
sudo install -m 0644 deploy/systemd/agentrelay.service /etc/systemd/system/agentrelay.service
sudo install -m 0640 deploy/systemd/agentrelay.env.example /etc/agentrelay/environment
sudo editor /etc/agentrelay/environment
```

The service is equivalent to:

```ini
[Unit]
Description=AgentRelay Multi-Agent Relay
After=network.target

[Service]
Type=simple
User=agentrelay
Group=agentrelay
WorkingDirectory=/var/lib/agentrelay

Environment="AGENTRELAY_TOKEN_FILE=/etc/agentrelay/token"
Environment="AGENTRELAY_DB_PATH=/var/lib/agentrelay/relay.db"
Environment="AGENTRELAY_HOST=127.0.0.1"
Environment="AGENTRELAY_PORT=8899"
Environment="AGENTRELAY_MSG_TTL_HOURS=72"

# Optional Telegram control
# Environment="AGENTRELAY_TG_TOKEN=123456:ABC..."
# Environment="AGENTRELAY_TG_CHAT=-100123..."

ExecStart=/usr/local/bin/agentrelay start
Restart=always
RestartSec=5

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/agentrelay

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable agentrelay
sudo systemctl start agentrelay
sudo systemctl status agentrelay
```

View logs:

```bash
sudo journalctl -u agentrelay -f
```

## One-Command Sidecar Install

For a user-level sidecar on a VPS, the repository includes
[deploy/install.sh](deploy/install.sh). It updates the source checkout, creates a
local venv, writes a loopback-only user systemd service, and verifies `/`.

```bash
bash deploy/install.sh
```

To enable Telegram supervisor mode during install:

```bash
AGENTRELAY_ENABLE_SUPERVISOR=1 bash deploy/install.sh
```

The installer prompts for the supervisor bot token and group chat ID unless
`AGENTRELAY_SUPERVISOR_TG_TOKEN` and `AGENTRELAY_SUPERVISOR_TG_CHAT` are already
set. Existing live agent bots keep their own services and tokens; only the
supervisor bot token is used by AgentRelay.

---

## OpenClaw Group Onboarding

Existing OpenClaw Telegram agents ignore new groups until the group is present
in `openclaw.json`. Do not hand-edit that config for AgentRelay labs. Use the
OpenClaw onboarder:

```bash
python deploy/openclaw-onboard-group.py \
  --group-chat-id -5135334366 \
  --accounts aarya,bheem \
  --supervisor-user-id 8926504741
```

The default is a dry run. If the planned changes are only the expected group and
allowlist entries, apply them and restart the gateway:

```bash
python deploy/openclaw-onboard-group.py \
  --group-chat-id -5135334366 \
  --accounts aarya,bheem \
  --supervisor-user-id 8926504741 \
  --apply \
  --restart
```

The script writes a timestamped backup next to `openclaw.json` before applying.
This is the first plug-in point for AgentRelay: host-specific adapters should
patch the minimum safe config automatically, with dry-run output and rollback
breadcrumbs.

## Live Test Notes

The first VPS lab exposed three required behaviors:

- The user systemd service must load the AgentRelay environment file; otherwise
  the HTTP server starts but supervisor mode never starts.
- Telegram permits only one active `getUpdates` consumer per bot token; manual
  debugging polls can cause HTTP 409 conflicts while the service is polling.
- OpenClaw agents do not join arbitrary new groups automatically. The new group
  must be registered under the global Telegram config and each selected account
  before Aarya/Bheem see group messages.

---

## Log Rotation

AgentRelay logs to stdout (captured by systemd journal). For persistent logs, configure `journald`:

```bash
# /etc/systemd/journald.conf.d/agentrelay.conf
[Journal]
SystemMaxUse=500M
MaxFileSec=1week
```

If you need file-based logs, run AgentRelay under a process supervisor that captures stdout and handles rotation.

---

## SQLite Notes

- AgentRelay uses a single SQLite database with WAL mode enabled.
- **Only one process** should write to the database at a time. Do not run multiple AgentRelay instances pointing to the same `.db` file.
- WAL mode creates `-wal` and `-shm` sidecar files. Keep them in the same directory.
- Back up `/var/lib/agentrelay/relay.db` and its `-wal` file together.

---

## Windows Deployment Notes

AgentRelay runs on Windows, but with caveats:

- `token_path.chmod(0o600)` on Windows only sets the DOS read-only flag. The token file is readable by any process running under the same Windows user account.
- **Recommendation on Windows:** Use the `AGENTRELAY_TOKEN` environment variable instead of the auto-generated token file, especially on shared machines.
- Reverse proxy setup on Windows IIS is possible but less tested. Caddy for Windows is the simplest TLS option.
- systemd is not available. Use Windows Services or Task Scheduler for auto-start.

---

## Verification Checklist

Before calling a deployment "production ready":

- [ ] TLS terminated at reverse proxy (Caddy or nginx)
- [ ] Port `8899` not exposed to public internet
- [ ] Token stored in `AGENTRELAY_TOKEN_FILE` or env var, not in logs
- [ ] `agentrelay start` does **not** print the actual token to stdout
- [ ] Dedicated non-root user running the service
- [ ] systemd service configured with restart and sandboxing
- [ ] Firewall active (UFW or equivalent)
- [ ] Backup plan for `/var/lib/agentrelay/relay.db`
- [ ] `journalctl -u agentrelay` shows clean startup
- [ ] WebSocket `/ws` connects and auth frame works through the proxy

---

*Generated for AgentRelay v2.1.0.*
