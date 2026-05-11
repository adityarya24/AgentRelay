from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agentrelay.cli import main
from agentrelay.models import Identity, IdentityKind, IdentityTransport, TrustLevel
from agentrelay.storage import Storage


@pytest.mark.asyncio
async def test_identity_storage_roundtrip_and_disable():
    with tempfile.TemporaryDirectory() as td:
        storage = Storage(str(Path(td) / "relay.db"))
        await storage.init()
        try:
            saved = await storage.upsert_identity(
                Identity(
                    id="openclaw.aarya",
                    display_name="Aarya",
                    kind=IdentityKind.VPS,
                    transport=IdentityTransport.MCP,
                    capabilities=["coordinate", "delegate"],
                    metadata={"runtime": "openclaw"},
                )
            )
            assert saved.id == "openclaw.aarya"
            assert saved.enabled is True

            listed = await storage.list_identities()
            assert [identity.id for identity in listed] == ["openclaw.aarya"]

            disabled = await storage.set_identity_enabled("openclaw.aarya", False)
            assert disabled is not None
            assert disabled.enabled is False
            assert await storage.list_identities() == []
            assert [identity.id for identity in await storage.list_identities(True)] == [
                "openclaw.aarya"
            ]
        finally:
            await storage.close()


def test_identity_cli_adds_external_identity_disabled_by_default(capsys):
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "relay.db")
        main(
            [
                "identities",
                "--db-path",
                db_path,
                "add",
                "external.somebot",
                "--display-name",
                "Some Bot",
                "--kind",
                "external",
                "--transport",
                "telegram",
                "--capability",
                "chat",
                "--metadata",
                "owner=test",
                "--json",
            ]
        )
        saved = json.loads(capsys.readouterr().out)
        assert saved["id"] == "external.somebot"
        assert saved["enabled"] is False
        assert saved["trust_level"] == TrustLevel.SANDBOX.value
        assert saved["metadata"] == {"owner": "test"}

        main(["identities", "--db-path", db_path, "list", "--json"])
        assert json.loads(capsys.readouterr().out) == []

        main(["identities", "--db-path", db_path, "list", "--all", "--json"])
        listed = json.loads(capsys.readouterr().out)
        assert [identity["id"] for identity in listed] == ["external.somebot"]

        main(["identities", "--db-path", db_path, "enable", "external.somebot", "--json"])
        enabled = json.loads(capsys.readouterr().out)
        assert enabled["enabled"] is True
