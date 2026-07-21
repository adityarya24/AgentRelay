from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "openclaw-onboard-group.py"


def _sample_config() -> dict:
    return {
        "channels": {
            "telegram": {
                "groups": {},
                "groupAllowFrom": ["8589044974"],
                "accounts": {
                    "aarya": {"groups": {}, "allowFrom": ["8589044974"]},
                    "bheem": {"groups": {}, "allowFrom": ["8589044974"]},
                },
            }
        }
    }


def test_openclaw_onboard_group_dry_run_does_not_write(tmp_path):
    config = tmp_path / "openclaw.json"
    original = _sample_config()
    config.write_text(json.dumps(original), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "--group-chat-id",
            "-5135334366",
            "--accounts",
            "aarya,bheem",
            "--supervisor-user-id",
            "8926504741",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Dry run only" in result.stdout
    assert json.loads(config.read_text(encoding="utf-8")) == original


def test_openclaw_onboard_group_apply_patches_minimum_config(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps(_sample_config()), encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "--group-chat-id",
            "-5135334366",
            "--accounts",
            "aarya,bheem",
            "--supervisor-user-id",
            "8926504741",
            "--apply",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    patched = json.loads(config.read_text(encoding="utf-8"))
    telegram = patched["channels"]["telegram"]
    assert telegram["groups"]["-5135334366"] == {"requireMention": False}
    assert "8926504741" in telegram["groupAllowFrom"]
    assert telegram["accounts"]["aarya"]["groups"]["-5135334366"] == {"requireMention": True}
    assert telegram["accounts"]["bheem"]["groups"]["-5135334366"] == {"requireMention": True}
    assert "8926504741" in telegram["accounts"]["aarya"]["allowFrom"]
    assert "8926504741" in telegram["accounts"]["bheem"]["allowFrom"]
    assert list(tmp_path.glob("openclaw.json.bak-agentrelay-*"))
