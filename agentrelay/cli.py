"""AgentRelay command line interface."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import uvicorn

from . import __version__
from agentrelay.plugins.telegram.models import (
    TelegramBotRegistration,
    TelegramGroupRegistration,
)
from .models import (
    Identity,
    IdentityKind,
    IdentityTransport,
    PolicyEffect,
    PolicyRule,
    TrustLevel,
)
from .relay import create_app, default_db_path, ensure_token
from .storage import Storage

DEFAULT_URL = "ws://localhost:8899"


def _token_file_path() -> Path:
    token_file = os.getenv("AGENTRELAY_TOKEN_FILE")
    return Path(token_file).expanduser() if token_file else Path.home() / ".agentrelay" / "token"


def _load_toml(path: str | None) -> dict:
    if not path:
        path = str(Path.home() / ".agentrelay" / "config.toml")
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    import tomllib

    return tomllib.loads(p.read_text())


def _config_value(cli_value, env_key: str, config: dict, section: str, key: str, default):
    if cli_value is not None:
        return cli_value
    if os.getenv(env_key) is not None:
        return os.getenv(env_key)
    return config.get(section, {}).get(key, default)


def cmd_start(args: argparse.Namespace) -> None:
    cfg = _load_toml(args.config)
    host = str(_config_value(args.host, "AGENTRELAY_HOST", cfg, "server", "host", "0.0.0.0"))
    port = int(_config_value(args.port, "AGENTRELAY_PORT", cfg, "server", "port", 8899))
    token = ensure_token(args.token)
    os.environ.setdefault("AGENTRELAY_TOKEN", token)
    if args.telegram_token:
        os.environ["AGENTRELAY_TG_TOKEN"] = args.telegram_token
    if args.telegram_chat:
        os.environ["AGENTRELAY_TG_CHAT"] = args.telegram_chat
    if args.telegram_transport:
        os.environ["AGENTRELAY_TELEGRAM_TRANSPORT"] = "1"
    if getattr(args, "telegram_supervisor", False):
        os.environ["AGENTRELAY_TELEGRAM_SUPERVISOR"] = "1"
    if getattr(args, "supervisor_token", None):
        os.environ["AGENTRELAY_SUPERVISOR_TG_TOKEN"] = args.supervisor_token
    if getattr(args, "supervisor_chat", None):
        os.environ["AGENTRELAY_SUPERVISOR_TG_CHAT"] = args.supervisor_chat
    if getattr(args, "supervisor_room", None):
        os.environ["AGENTRELAY_SUPERVISOR_ROOM"] = args.supervisor_room
    if getattr(args, "supervisor_moderate", False):
        os.environ["AGENTRELAY_SUPERVISOR_MODERATE"] = "1"
    if getattr(args, "supervisor_cooldown", None) is not None:
        os.environ["AGENTRELAY_SUPERVISOR_COOLDOWN"] = str(args.supervisor_cooldown)
    if getattr(args, "supervisor_agent_cap", None) is not None:
        os.environ["AGENTRELAY_SUPERVISOR_AGENT_CAP"] = str(args.supervisor_agent_cap)
    db_path = args.db_path or os.getenv("AGENTRELAY_DB_PATH") or default_db_path()
    logging.basicConfig(
        level=getattr(logging, str(cfg.get("logging", {}).get("level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    print(f"AgentRelay v{__version__} listening on {host}:{port}")
    token_path = _token_file_path()
    print(f"Token file: {token_path}")
    print("Token env:  AGENTRELAY_TOKEN")
    from .relay import Relay

    app = create_app(Relay(token=token, db_path=db_path))
    uvicorn.run(app, host=host, port=port)


def cmd_token(args: argparse.Namespace) -> None:
    if args.path:
        print(str(_token_file_path()))
        return
    token = ensure_token(args.token)
    if args.quiet:
        print(token)
    else:
        print(token)


def cmd_version(_: argparse.Namespace) -> None:
    print(__version__)


def _parse_kv(items: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"metadata must be KEY=VALUE, got {item!r}")
        parsed[key] = value
    return parsed


def _parse_bot_spec(spec: str) -> tuple[str, str, str]:
    identity, sep, rest = spec.partition("=")
    username, token_sep, token_env = rest.partition(":")
    if not sep or not token_sep or not identity or not username or not token_env:
        raise ValueError("BOT must look like identity=@BotUsername:ENV_VAR")
    return identity.strip(), username.strip().removeprefix("@"), token_env.strip()


def _identity_json(identity: Identity) -> str:
    return json.dumps(identity.model_dump(mode="json"), ensure_ascii=False)


async def _identity_storage(args: argparse.Namespace) -> Storage:
    storage = Storage(args.db_path or os.getenv("AGENTRELAY_DB_PATH") or default_db_path())
    await storage.init()
    return storage


def _default_trust(identity_id: str, kind: IdentityKind) -> TrustLevel:
    if kind == IdentityKind.EXTERNAL or identity_id.startswith("external."):
        return TrustLevel.SANDBOX
    return TrustLevel.TRUSTED


def _default_enabled(identity_id: str, kind: IdentityKind, explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return not (kind == IdentityKind.EXTERNAL or identity_id.startswith("external."))


def _telegram_identity_kind(identity_id: str) -> IdentityKind:
    return IdentityKind.EXTERNAL if identity_id.startswith("external.") else IdentityKind.VPS


async def _identities_add(args: argparse.Namespace) -> None:
    storage = await _identity_storage(args)
    try:
        kind = IdentityKind(args.kind)
        trust_level = TrustLevel(args.trust_level) if args.trust_level else _default_trust(args.id, kind)
        identity = Identity(
            id=args.id,
            display_name=args.display_name or args.id,
            kind=kind,
            transport=IdentityTransport(args.transport),
            capabilities=sorted(set(args.capability or [])),
            enabled=_default_enabled(args.id, kind, args.enabled),
            trust_level=trust_level,
            metadata=_parse_kv(args.metadata),
            secret_ref=args.secret_ref,
        )
        saved = await storage.upsert_identity(identity)
        print(_identity_json(saved) if args.json else f"saved {saved.id} ({saved.trust_level.value})")
    finally:
        await storage.close()


async def _identities_list(args: argparse.Namespace) -> None:
    storage = await _identity_storage(args)
    try:
        identities = await storage.list_identities(include_disabled=args.all)
        if args.json:
            print(json.dumps([i.model_dump(mode="json") for i in identities], ensure_ascii=False))
            return
        for identity in identities:
            status = "enabled" if identity.enabled else "disabled"
            caps = ",".join(identity.capabilities) if identity.capabilities else "-"
            print(
                f"{identity.id}\t{identity.display_name}\t{identity.kind.value}\t"
                f"{identity.transport.value}\t{identity.trust_level.value}\t{status}\t{caps}"
            )
    finally:
        await storage.close()


async def _identities_show(args: argparse.Namespace) -> None:
    storage = await _identity_storage(args)
    try:
        identity = await storage.get_identity(args.id)
        if identity is None:
            raise KeyError(args.id)
        print(_identity_json(identity))
    finally:
        await storage.close()


async def _identities_set_enabled(args: argparse.Namespace, enabled: bool) -> None:
    storage = await _identity_storage(args)
    try:
        identity = await storage.set_identity_enabled(args.id, enabled)
        if identity is None:
            raise KeyError(args.id)
        print(_identity_json(identity) if args.json else f"{identity.id} {'enabled' if enabled else 'disabled'}")
    finally:
        await storage.close()


def cmd_identities(args: argparse.Namespace) -> None:
    try:
        if args.identities_cmd == "add":
            asyncio.run(_identities_add(args))
        elif args.identities_cmd == "list":
            asyncio.run(_identities_list(args))
        elif args.identities_cmd == "show":
            asyncio.run(_identities_show(args))
        elif args.identities_cmd == "enable":
            asyncio.run(_identities_set_enabled(args, True))
        elif args.identities_cmd == "disable":
            asyncio.run(_identities_set_enabled(args, False))
        else:
            raise ValueError("missing identities command")
    except Exception as exc:
        print(f"agentrelay identities failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _policy_json(rule: PolicyRule) -> str:
    return json.dumps(rule.model_dump(mode="json"), ensure_ascii=False)


async def _policy_add(args: argparse.Namespace, effect: PolicyEffect) -> None:
    storage = await _identity_storage(args)
    try:
        rule = PolicyRule(
            source=args.source,
            target=args.target,
            room=args.room,
            effect=effect,
            reason=args.reason,
        )
        saved = await storage.upsert_policy(rule)
        print(_policy_json(saved) if args.json else f"{saved.effect.value} {saved.source} -> {saved.target}")
    finally:
        await storage.close()


async def _policy_list(args: argparse.Namespace) -> None:
    storage = await _identity_storage(args)
    try:
        rules = await storage.list_policies(include_disabled=args.all)
        if args.json:
            print(json.dumps([rule.model_dump(mode="json") for rule in rules], ensure_ascii=False))
            return
        for rule in rules:
            status = "enabled" if rule.enabled else "disabled"
            print(
                f"{rule.id}\t{rule.effect.value}\t{rule.source}\t{rule.target}\t"
                f"{rule.room}\t{status}\t{rule.reason or '-'}"
            )
    finally:
        await storage.close()


async def _policy_set_enabled(args: argparse.Namespace, enabled: bool) -> None:
    storage = await _identity_storage(args)
    try:
        rule = await storage.set_policy_enabled(args.id, enabled)
        if rule is None:
            raise KeyError(args.id)
        print(_policy_json(rule) if args.json else f"{rule.id} {'enabled' if enabled else 'disabled'}")
    finally:
        await storage.close()


def cmd_policy(args: argparse.Namespace) -> None:
    try:
        if args.policy_cmd == "allow":
            asyncio.run(_policy_add(args, PolicyEffect.ALLOW))
        elif args.policy_cmd == "deny":
            asyncio.run(_policy_add(args, PolicyEffect.DENY))
        elif args.policy_cmd == "list":
            asyncio.run(_policy_list(args))
        elif args.policy_cmd == "enable":
            asyncio.run(_policy_set_enabled(args, True))
        elif args.policy_cmd == "disable":
            asyncio.run(_policy_set_enabled(args, False))
        else:
            raise ValueError("missing policy command")
    except Exception as exc:
        print(f"agentrelay policy failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _telegram_bot_json(bot: TelegramBotRegistration) -> str:
    return json.dumps(bot.model_dump(mode="json"), ensure_ascii=False)


def _telegram_group_json(group: TelegramGroupRegistration) -> str:
    return json.dumps(group.model_dump(mode="json"), ensure_ascii=False)


async def _telegram_add_bot(args: argparse.Namespace) -> None:
    storage = await _identity_storage(args)
    try:
        token_ref = args.token_ref or f"env:{args.token_env}"
        bot = TelegramBotRegistration(
            identity_id=args.identity_id,
            username=args.username.removeprefix("@"),
            token_ref=token_ref,
            bot_to_bot_enabled=args.bot_to_bot,
            enabled=not args.disabled,
        )
        saved = await storage.upsert_telegram_bot(bot)
        if await storage.get_identity(args.identity_id) is None:
            kind = _telegram_identity_kind(args.identity_id)
            identity = Identity(
                id=args.identity_id,
                display_name=args.identity_id,
                kind=kind,
                transport=IdentityTransport.TELEGRAM,
                enabled=_default_enabled(args.identity_id, kind, None),
                trust_level=_default_trust(args.identity_id, kind),
                secret_ref=token_ref,
            )
            await storage.upsert_identity(identity)
        print(_telegram_bot_json(saved) if args.json else f"saved @{saved.username} -> {saved.identity_id}")
    finally:
        await storage.close()


async def _telegram_list_bots(args: argparse.Namespace) -> None:
    storage = await _identity_storage(args)
    try:
        bots = await storage.list_telegram_bots(include_disabled=args.all)
        if args.json:
            print(json.dumps([bot.model_dump(mode="json") for bot in bots], ensure_ascii=False))
            return
        for bot in bots:
            status = "enabled" if bot.enabled else "disabled"
            b2b = "bot-to-bot" if bot.bot_to_bot_enabled else "bot-to-bot-missing"
            print(f"{bot.identity_id}\t@{bot.username}\t{bot.token_ref}\t{status}\t{b2b}")
    finally:
        await storage.close()


async def _telegram_add_group(args: argparse.Namespace) -> None:
    storage = await _identity_storage(args)
    try:
        group = TelegramGroupRegistration(
            room=args.room,
            chat_id=str(args.chat_id),
            title=args.title,
            enabled=not args.disabled,
        )
        saved = await storage.upsert_telegram_group(group)
        print(_telegram_group_json(saved) if args.json else f"saved {saved.room} -> {saved.chat_id}")
    finally:
        await storage.close()


async def _telegram_list_groups(args: argparse.Namespace) -> None:
    storage = await _identity_storage(args)
    try:
        groups = await storage.list_telegram_groups(include_disabled=args.all)
        if args.json:
            print(json.dumps([group.model_dump(mode="json") for group in groups], ensure_ascii=False))
            return
        for group in groups:
            status = "enabled" if group.enabled else "disabled"
            print(f"{group.room}\t{group.chat_id}\t{status}\t{group.title or '-'}")
    finally:
        await storage.close()


async def _telegram_verify_all(args: argparse.Namespace) -> None:
    storage = await _identity_storage(args)
    try:
        rows = []
        for bot in await storage.list_telegram_bots(include_disabled=True):
            checks = {
                "identity_id": bot.identity_id,
                "username": bot.username,
                "enabled": bot.enabled,
                "bot_to_bot_enabled": bot.bot_to_bot_enabled,
                "token_ref": bot.token_ref,
                "token_available": False,
                "ready": False,
                "issues": [],
            }
            if bot.token_ref.startswith("env:"):
                env_name = bot.token_ref.split(":", 1)[1]
                checks["token_available"] = bool(os.getenv(env_name))
                if not checks["token_available"]:
                    checks["issues"].append(f"missing env:{env_name}")
            else:
                checks["issues"].append("token_ref must be env:NAME for local verification")
            if not bot.enabled:
                checks["issues"].append("bot disabled")
            if not bot.bot_to_bot_enabled:
                checks["issues"].append("enable Bot-to-Bot Communication Mode in BotFather")
            checks["ready"] = not checks["issues"]
            rows.append(checks)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
            return
        for row in rows:
            verdict = "ready" if row["ready"] else "not-ready"
            issues = ", ".join(row["issues"]) if row["issues"] else "-"
            print(f"{row['identity_id']}\t@{row['username']}\t{verdict}\t{issues}")
    finally:
        await storage.close()


async def _init_telegram_lab(args: argparse.Namespace) -> None:
    if not args.db_path and args.init_db_path:
        args.db_path = args.init_db_path
    parsed = [_parse_bot_spec(spec) for spec in args.bot]
    if len(args.bot or []) < 2:
        raise ValueError("telegram-lab requires at least two --bot entries")
    storage = await _identity_storage(args)
    try:
        room = args.room
        if args.group_chat_id:
            await storage.upsert_telegram_group(
                TelegramGroupRegistration(
                    room=room,
                    chat_id=str(args.group_chat_id),
                    title=args.group_title,
                )
            )
        for identity_id, username, token_env in parsed:
            token_ref = f"env:{token_env}"
            kind = _telegram_identity_kind(identity_id)
            enabled = _default_enabled(identity_id, kind, None)
            await storage.upsert_identity(
                Identity(
                    id=identity_id,
                    display_name=identity_id,
                    kind=kind,
                    transport=IdentityTransport.TELEGRAM,
                    capabilities=["telegram", "bot-to-bot"],
                    enabled=enabled,
                    trust_level=_default_trust(identity_id, kind),
                    secret_ref=token_ref,
                )
            )
            await storage.upsert_telegram_bot(
                TelegramBotRegistration(
                    identity_id=identity_id,
                    username=username,
                    token_ref=token_ref,
                    bot_to_bot_enabled=True,
                    enabled=enabled,
                )
            )
        policy_count = 0
        if args.allow_pairwise:
            identities = [
                identity_id
                for identity_id, _, _ in parsed
                if _default_enabled(identity_id, _telegram_identity_kind(identity_id), None)
            ]
            for source in identities:
                for target in identities:
                    if source == target:
                        continue
                    await storage.upsert_policy(
                        PolicyRule(
                            source=source,
                            target=target,
                            room=room,
                            effect=PolicyEffect.ALLOW,
                            reason="telegram-lab onboarding",
                        )
                    )
                    policy_count += 1
        summary = {
            "room": room,
            "bots": [identity_id for identity_id, _, _ in parsed],
            "policies": policy_count,
            "next_command": "agentrelay start --telegram-transport",
        }
        if args.json:
            print(json.dumps(summary, ensure_ascii=False))
        else:
            print(f"Initialized {room} with {len(parsed)} Telegram bots.")
            print("BotFather: enable Bot-to-Bot Communication Mode for every bot.")
            print("Environment: export each token env var before starting AgentRelay.")
            print("Start: agentrelay start --telegram-transport")
    finally:
        await storage.close()


def cmd_init(args: argparse.Namespace) -> None:
    try:
        if args.init_cmd == "telegram-lab":
            asyncio.run(_init_telegram_lab(args))
        else:
            raise ValueError("missing init command")
    except Exception as exc:
        print(f"agentrelay init failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def cmd_telegram(args: argparse.Namespace) -> None:
    try:
        if args.telegram_cmd == "add-bot":
            asyncio.run(_telegram_add_bot(args))
        elif args.telegram_cmd == "list-bots":
            asyncio.run(_telegram_list_bots(args))
        elif args.telegram_cmd == "add-group":
            asyncio.run(_telegram_add_group(args))
        elif args.telegram_cmd == "list-groups":
            asyncio.run(_telegram_list_groups(args))
        elif args.telegram_cmd == "verify-all":
            asyncio.run(_telegram_verify_all(args))
        else:
            raise ValueError("missing telegram command")
    except Exception as exc:
        print(f"agentrelay telegram failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _normalize_ws_url(url: str) -> str:
    base = (url or DEFAULT_URL).rstrip("/")
    if base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    return base


async def _topic(args: argparse.Namespace) -> None:
    import websockets

    token = args.token or ensure_token()
    room = args.room or "general"
    agent = args.agent or "cli"
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if not agents:
        raise ValueError("--agents must include at least one agent id")
    thread = args.thread or args.topic[:48]
    url = f"{_normalize_ws_url(args.url)}/ws"
    payload = {"content": args.topic, "thread": thread, "mentions": agents}
    async with websockets.connect(url, open_timeout=10) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "agent": agent, "room": room}))
        await ws.send(json.dumps(payload))
        deadline = asyncio.get_running_loop().time() + args.timeout
        last = None
        while asyncio.get_running_loop().time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - asyncio.get_running_loop().time()))
            msg = json.loads(raw)
            last = msg
            if msg.get("content") == "ack" or msg.get("error"):
                print(json.dumps(msg, ensure_ascii=False))
                return
        print(json.dumps(last or {"status": "sent", "thread": thread}, ensure_ascii=False))


def cmd_topic(args: argparse.Namespace) -> None:
    try:
        asyncio.run(_topic(args))
    except Exception as exc:
        print(f"agentrelay topic failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentrelay")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="initialize AgentRelay onboarding profiles")
    init.add_argument("--db-path", dest="init_db_path")
    init_sub = init.add_subparsers(dest="init_cmd", required=True)
    telegram_lab = init_sub.add_parser(
        "telegram-lab",
        help="initialize a supervised Telegram bot-to-bot lab",
    )
    telegram_lab.add_argument("--db-path")
    telegram_lab.add_argument("--room", default="telegram:lab")
    telegram_lab.add_argument("--group-chat-id")
    telegram_lab.add_argument("--group-title", default="AgentRelay Lab")
    telegram_lab.add_argument("--bot", action="append", default=[], metavar="IDENTITY=@BOT:ENV")
    telegram_lab.add_argument(
        "--allow-pairwise",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    telegram_lab.add_argument("--json", action="store_true")
    telegram_lab.set_defaults(func=cmd_init)

    start = sub.add_parser("start", help="start relay server")
    start.add_argument("--host")
    start.add_argument("--port", type=int)
    start.add_argument("--token")
    start.add_argument("--telegram-token")
    start.add_argument("--telegram-chat")
    start.add_argument("--telegram-transport", action="store_true")
    start.add_argument("--telegram-supervisor", action="store_true")
    start.add_argument("--supervisor-token")
    start.add_argument("--supervisor-chat")
    start.add_argument("--supervisor-room")
    start.add_argument("--supervisor-moderate", action="store_true")
    start.add_argument("--supervisor-cooldown", type=int)
    start.add_argument("--supervisor-agent-cap", type=int)
    start.add_argument("--db-path")
    start.add_argument("--config")
    start.set_defaults(func=cmd_start)

    topic = sub.add_parser("topic", help="create a discussion topic")
    topic.add_argument("topic")
    topic.add_argument("--agents", required=True)
    topic.add_argument("--room", default="general")
    topic.add_argument("--url", default=os.getenv("AGENTRELAY_URL", DEFAULT_URL))
    topic.add_argument("--token")
    topic.add_argument("--agent", default="cli")
    topic.add_argument("--thread")
    topic.add_argument("--timeout", type=float, default=10.0)
    topic.set_defaults(func=cmd_topic)

    token = sub.add_parser("token", help="print current token, generating one if missing")
    token.add_argument("--token", help="print this explicit token instead of discovering one")
    token.add_argument("--path", action="store_true", help="print token file path")
    token.add_argument("--quiet", action="store_true", help="print token only")
    token.set_defaults(func=cmd_token)

    version = sub.add_parser("version", help="print version")
    version.set_defaults(func=cmd_version)

    identities = sub.add_parser("identities", help="manage durable participant identities")
    identities.add_argument("--db-path")
    identities_sub = identities.add_subparsers(dest="identities_cmd", required=True)

    identity_add = identities_sub.add_parser("add", help="add or update an identity")
    identity_add.add_argument("id")
    identity_add.add_argument("--display-name")
    identity_add.add_argument(
        "--kind",
        choices=[kind.value for kind in IdentityKind],
        default=IdentityKind.LOCAL.value,
    )
    identity_add.add_argument(
        "--transport",
        choices=[transport.value for transport in IdentityTransport],
        default=IdentityTransport.MCP.value,
    )
    identity_add.add_argument("--capability", action="append", default=[])
    identity_add.add_argument("--metadata", action="append", default=[], metavar="KEY=VALUE")
    identity_add.add_argument("--secret-ref")
    identity_add.add_argument("--trust-level", choices=[level.value for level in TrustLevel])
    identity_enabled = identity_add.add_mutually_exclusive_group()
    identity_enabled.add_argument("--enable", dest="enabled", action="store_true")
    identity_enabled.add_argument("--disable", dest="enabled", action="store_false")
    identity_add.set_defaults(func=cmd_identities, enabled=None)
    identity_add.add_argument("--json", action="store_true")

    identity_list = identities_sub.add_parser("list", help="list identities")
    identity_list.add_argument("--all", action="store_true", help="include disabled identities")
    identity_list.add_argument("--json", action="store_true")
    identity_list.set_defaults(func=cmd_identities)

    identity_show = identities_sub.add_parser("show", help="show one identity as JSON")
    identity_show.add_argument("id")
    identity_show.set_defaults(func=cmd_identities)

    identity_enable = identities_sub.add_parser("enable", help="enable an identity")
    identity_enable.add_argument("id")
    identity_enable.add_argument("--json", action="store_true")
    identity_enable.set_defaults(func=cmd_identities)

    identity_disable = identities_sub.add_parser("disable", help="disable an identity")
    identity_disable.add_argument("id")
    identity_disable.add_argument("--json", action="store_true")
    identity_disable.set_defaults(func=cmd_identities)

    policy = sub.add_parser("policy", help="manage route policy rules")
    policy.add_argument("--db-path")
    policy_sub = policy.add_subparsers(dest="policy_cmd", required=True)

    for name in ("allow", "deny"):
        route = policy_sub.add_parser(name, help=f"add a {name} route rule")
        route.add_argument("source")
        route.add_argument("target")
        route.add_argument("--room", default="*")
        route.add_argument("--reason")
        route.add_argument("--json", action="store_true")
        route.set_defaults(func=cmd_policy)

    policy_list = policy_sub.add_parser("list", help="list policy rules")
    policy_list.add_argument("--all", action="store_true", help="include disabled rules")
    policy_list.add_argument("--json", action="store_true")
    policy_list.set_defaults(func=cmd_policy)

    policy_enable = policy_sub.add_parser("enable", help="enable a policy rule")
    policy_enable.add_argument("id")
    policy_enable.add_argument("--json", action="store_true")
    policy_enable.set_defaults(func=cmd_policy)

    policy_disable = policy_sub.add_parser("disable", help="disable a policy rule")
    policy_disable.add_argument("id")
    policy_disable.add_argument("--json", action="store_true")
    policy_disable.set_defaults(func=cmd_policy)

    telegram = sub.add_parser("telegram", help="manage Telegram bot-to-bot transport")
    telegram.add_argument("--db-path")
    telegram_sub = telegram.add_subparsers(dest="telegram_cmd", required=True)

    tg_add = telegram_sub.add_parser("add-bot", help="register a Telegram bot identity")
    tg_add.add_argument("identity_id")
    tg_add.add_argument("--username", required=True)
    token_group = tg_add.add_mutually_exclusive_group(required=True)
    token_group.add_argument("--token-env")
    token_group.add_argument("--token-ref")
    tg_add.add_argument("--bot-to-bot", action="store_true")
    tg_add.add_argument("--disabled", action="store_true")
    tg_add.add_argument("--json", action="store_true")
    tg_add.set_defaults(func=cmd_telegram)

    tg_list = telegram_sub.add_parser("list-bots", help="list registered Telegram bots")
    tg_list.add_argument("--all", action="store_true", help="include disabled bots")
    tg_list.add_argument("--json", action="store_true")
    tg_list.set_defaults(func=cmd_telegram)

    tg_group = telegram_sub.add_parser("add-group", help="register a Telegram group as a room")
    tg_group.add_argument("room")
    tg_group.add_argument("--chat-id", required=True)
    tg_group.add_argument("--title")
    tg_group.add_argument("--disabled", action="store_true")
    tg_group.add_argument("--json", action="store_true")
    tg_group.set_defaults(func=cmd_telegram)

    tg_groups = telegram_sub.add_parser("list-groups", help="list registered Telegram groups")
    tg_groups.add_argument("--all", action="store_true", help="include disabled groups")
    tg_groups.add_argument("--json", action="store_true")
    tg_groups.set_defaults(func=cmd_telegram)

    tg_verify = telegram_sub.add_parser("verify-all", help="check local Telegram readiness")
    tg_verify.add_argument("--json", action="store_true")
    tg_verify.set_defaults(func=cmd_telegram)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
