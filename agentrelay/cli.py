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
from .models import Identity, IdentityKind, IdentityTransport, TrustLevel
from .relay import create_app, default_db_path, ensure_token
from .storage import Storage

DEFAULT_URL = "ws://localhost:8899"


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
    db_path = args.db_path or os.getenv("AGENTRELAY_DB_PATH") or default_db_path()
    logging.basicConfig(
        level=getattr(logging, str(cfg.get("logging", {}).get("level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    print(f"AgentRelay v{__version__} listening on {host}:{port}")
    print(f"Token: {token}")
    from .relay import Relay

    app = create_app(Relay(token=token, db_path=db_path))
    uvicorn.run(app, host=host, port=port)


def cmd_token(args: argparse.Namespace) -> None:
    token = ensure_token(args.token)
    if args.path:
        print(str(Path.home() / ".agentrelay" / "token"))
    elif args.quiet:
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

    start = sub.add_parser("start", help="start relay server")
    start.add_argument("--host")
    start.add_argument("--port", type=int)
    start.add_argument("--token")
    start.add_argument("--telegram-token")
    start.add_argument("--telegram-chat")
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
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
