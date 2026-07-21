#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${AGENTRELAY_REPO_URL:-https://github.com/adityarya24/AgentRelay.git}"
APP_DIR="${AGENTRELAY_APP_DIR:-$HOME/agentrelay-v2}"
STATE_DIR="${AGENTRELAY_STATE_DIR:-$HOME/.agentrelay-v2}"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_FILE="$SERVICE_DIR/agentrelay.service"
ENV_FILE="$STATE_DIR/environment"
TOKEN_FILE="$STATE_DIR/token"
BRANCH="${AGENTRELAY_BRANCH:-main}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

prompt_secret() {
  local name="$1"
  local value="${!name:-}"
  if [ -z "$value" ]; then
    read -r -s -p "$name: " value
    echo
  fi
  printf '%s' "$value"
}

prompt_value() {
  local name="$1"
  local value="${!name:-}"
  if [ -z "$value" ]; then
    read -r -p "$name: " value
  fi
  printf '%s' "$value"
}

need_cmd git
need_cmd python3
need_cmd systemctl

mkdir -p "$STATE_DIR" "$SERVICE_DIR"
chmod 700 "$STATE_DIR"

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -e "${APP_DIR}[all]"

if [ ! -f "$TOKEN_FILE" ]; then
  "$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"

SUPERVISOR_TOKEN="${AGENTRELAY_SUPERVISOR_TG_TOKEN:-}"
SUPERVISOR_CHAT="${AGENTRELAY_SUPERVISOR_TG_CHAT:-}"
if [ "${AGENTRELAY_ENABLE_SUPERVISOR:-}" = "1" ]; then
  SUPERVISOR_TOKEN="$(prompt_secret AGENTRELAY_SUPERVISOR_TG_TOKEN)"
  SUPERVISOR_CHAT="$(prompt_value AGENTRELAY_SUPERVISOR_TG_CHAT)"
fi

{
  echo "AGENTRELAY_TOKEN_FILE=$TOKEN_FILE"
  echo "AGENTRELAY_DB_PATH=$STATE_DIR/relay.db"
  echo "AGENTRELAY_HOST=127.0.0.1"
  echo "AGENTRELAY_PORT=${AGENTRELAY_PORT:-8899}"
  echo "AGENTRELAY_MSG_TTL_HOURS=${AGENTRELAY_MSG_TTL_HOURS:-72}"
  if [ -n "$SUPERVISOR_TOKEN" ] && [ -n "$SUPERVISOR_CHAT" ]; then
    echo "AGENTRELAY_TELEGRAM_SUPERVISOR=1"
    echo "AGENTRELAY_SUPERVISOR_TG_TOKEN=$SUPERVISOR_TOKEN"
    echo "AGENTRELAY_SUPERVISOR_TG_CHAT=$SUPERVISOR_CHAT"
    echo "AGENTRELAY_SUPERVISOR_ROOM=${AGENTRELAY_SUPERVISOR_ROOM:-telegram:lab}"
    echo "AGENTRELAY_SUPERVISOR_MODERATE=${AGENTRELAY_SUPERVISOR_MODERATE:-0}"
    echo "AGENTRELAY_SUPERVISOR_COOLDOWN=${AGENTRELAY_SUPERVISOR_COOLDOWN:-30}"
    echo "AGENTRELAY_SUPERVISOR_AGENT_CAP=${AGENTRELAY_SUPERVISOR_AGENT_CAP:-3}"
  fi
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=AgentRelay sidecar relay
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/agentrelay start
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now agentrelay.service
sleep 1

"$APP_DIR/.venv/bin/python" - <<'PY'
import json
import os
import urllib.request

port = os.getenv("AGENTRELAY_PORT", "8899")
print(json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3)))
PY

echo "AgentRelay installed at $APP_DIR"
echo "State dir: $STATE_DIR"
echo "Service: systemctl --user status agentrelay.service"
if [ -z "$SUPERVISOR_TOKEN" ] || [ -z "$SUPERVISOR_CHAT" ]; then
  echo "Supervisor mode is not enabled. Re-run with AGENTRELAY_ENABLE_SUPERVISOR=1 to configure it."
else
  echo "Supervisor mode enabled."
  echo "Telegram setup: add the supervisor bot to the group, make it admin for delete-message moderation, enable bot-to-bot mode on participating agent bots, then use /discuss."
fi
