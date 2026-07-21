#!/usr/bin/env python3
"""Onboard an OpenClaw Telegram group for AgentRelay supervisor labs.

This is intentionally narrow: it patches only OpenClaw Telegram group allowlists
for selected accounts and writes a timestamped backup before changing anything.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _ensure_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _add_unique(items: list[str], value: str) -> bool:
    if value in items:
        return False
    items.append(value)
    return True


def patch_config(
    cfg: dict[str, Any],
    *,
    group_chat_id: str,
    accounts: list[str],
    supervisor_user_id: str | None,
    require_mention: bool,
) -> list[str]:
    changes: list[str] = []
    telegram = cfg.setdefault("channels", {}).setdefault("telegram", {})
    groups = telegram.setdefault("groups", {})
    group = groups.setdefault(group_chat_id, {})
    if group.get("requireMention") is not False:
        group["requireMention"] = False
        changes.append(f"registered global Telegram group {group_chat_id}")

    group_allow_from = _ensure_list(telegram.get("groupAllowFrom"))
    if supervisor_user_id and _add_unique(group_allow_from, supervisor_user_id):
        changes.append(f"allowed supervisor {supervisor_user_id} globally for group messages")
    telegram["groupAllowFrom"] = group_allow_from

    account_map = telegram.setdefault("accounts", {})
    for account_name in accounts:
        account = account_map.get(account_name)
        if not isinstance(account, dict):
            raise KeyError(f"unknown OpenClaw Telegram account: {account_name}")

        account_groups = account.setdefault("groups", {})
        account_group = account_groups.setdefault(group_chat_id, {})
        if account_group.get("requireMention") != require_mention:
            account_group["requireMention"] = require_mention
            changes.append(
                f"registered group {group_chat_id} for account {account_name} "
                f"(requireMention={require_mention})"
            )

        allow_from = _ensure_list(account.get("allowFrom"))
        if supervisor_user_id and _add_unique(allow_from, supervisor_user_id):
            changes.append(f"allowed supervisor {supervisor_user_id} for account {account_name}")
        account["allowFrom"] = allow_from

    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/home/aditya/.openclaw/openclaw.json")
    parser.add_argument("--group-chat-id", required=True)
    parser.add_argument("--accounts", required=True, help="comma-separated OpenClaw account ids, e.g. aarya,bheem")
    parser.add_argument("--supervisor-user-id")
    parser.add_argument("--no-require-mention", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()

    path = Path(args.config).expanduser()
    cfg = _load(path)
    accounts = [item.strip() for item in args.accounts.split(",") if item.strip()]
    changes = patch_config(
        cfg,
        group_chat_id=str(args.group_chat_id),
        accounts=accounts,
        supervisor_user_id=str(args.supervisor_user_id) if args.supervisor_user_id else None,
        require_mention=not args.no_require_mention,
    )

    if not changes:
        print("No changes needed.")
    else:
        print("Planned changes:")
        for change in changes:
            print(f"- {change}")

    if not args.apply:
        print("Dry run only. Re-run with --apply to write changes.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-agentrelay-{stamp}")
    shutil.copy2(path, backup)
    _dump(path, cfg)
    print(f"Wrote {path}")
    print(f"Backup: {backup}")

    if args.restart:
        subprocess.run(["systemctl", "--user", "restart", "openclaw-gateway.service"], check=True)
        print("Restarted openclaw-gateway.service")
    else:
        print("Restart skipped. Restart openclaw-gateway.service for changes to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
