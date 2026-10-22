"""The command lines: `uu`, `python -m uu_cli` and the old brightspace-cli.py entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
BRIGHTSPACE_COMMANDS = ["save-creds", "login", "courses", "toc", "assignments", "grades",
                        "news", "schedule", "deadlines", "sync", "sync-all"]


def cli(*args: str, state: Path) -> subprocess.CompletedProcess:
    env = {**os.environ,
           "BRIGHTSPACE_SESSION_DIR": str(state / "session"),
           "BRIGHTSPACE_TOKEN_FILE": str(state / "token.json")}
    env.pop("BRIGHTSPACE_ORG_UNIT", None)
    return subprocess.run([sys.executable, *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=60)


def test_uu_lists_services(state):
    result = cli("-m", "uu_cli", "--help", state=state)

    assert result.returncode == 0
    assert "brightspace" in result.stdout and "konjoin" in result.stdout


def test_konjoin_help_lists_every_command(state):
    result = cli("-m", "uu_cli", "konjoin", "--help", state=state)

    assert result.returncode == 0
    for command in ["login", "projects", "project", "export"]:
        assert command in result.stdout


@pytest.mark.parametrize("service", ["library", "papers"])  # papers: the old name
def test_library_help_lists_every_command(state, service):
    result = cli("-m", "uu_cli", service, "--help", state=state)

    assert result.returncode == 0
    for command in ["open", "get", "watch", "elsevier"]:
        assert command in result.stdout


@pytest.mark.parametrize("entry", [["-m", "uu_cli", "brightspace"], ["brightspace-cli.py"]])
def test_brightspace_help_lists_every_command(state, entry):
    result = cli(*entry, "--help", state=state)

    assert result.returncode == 0
    for command in BRIGHTSPACE_COMMANDS:
        assert command in result.stdout


def test_unknown_service_is_an_error(state):
    result = cli("-m", "uu_cli", "moodle", state=state)

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_content_commands_need_an_org_unit(state):
    result = cli("-m", "uu_cli", "brightspace", "toc", state=state)

    assert result.returncode == 2
    assert "--org-unit or BRIGHTSPACE_ORG_UNIT required" in result.stderr


def test_totp_flag_rejects_a_six_digit_code(state):
    result = cli("brightspace-cli.py", "--totp", "123456", "courses", state=state)

    assert result.returncode == 2
    assert "expects your TOTP secret key" in result.stderr
