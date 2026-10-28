"""The Brightspace commands end to end, in a real Chromium, against the offline fake."""

from __future__ import annotations

import json
import stat
import time

import pytest

from fake_uu import ORG_UNIT, PASSWORD, PDF, SOLISID, TOTP_SECRET, make_jwt
from uu_cli import paths
from uu_cli.auth.credentials import save_creds
from uu_cli.services.brightspace import api
from uu_cli.services.brightspace.cli import main

pytestmark = pytest.mark.browser


def run(argv: list[str], capsys) -> tuple[object, str, str]:
    """Run `uu brightspace <argv>` in-process; return (exit code, stdout, stderr)."""
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out, err = capsys.readouterr()
    return exc.value.code, out, err


@pytest.fixture
def secret_env(monkeypatch):
    monkeypatch.setenv("BRIGHTSPACE_SOLISID", SOLISID)
    monkeypatch.setenv("BRIGHTSPACE_PASSWORD", PASSWORD)
    monkeypatch.setenv("BRIGHTSPACE_TOTP_SECRET", TOTP_SECRET)


def test_login_with_totp_secret_caches_token(fake_uu, secret_env, capsys):
    code, _, err = run(["login"], capsys)

    assert code == 0
    assert fake_uu.logins == 1 and len(fake_uu.codes) == 1
    assert json.loads(paths.TOKEN_FILE.read_text())["token"] == fake_uu.token
    assert stat.S_IMODE(paths.TOKEN_FILE.stat().st_mode) == 0o600
    assert "Token cached." in err


def test_login_with_saved_credentials(fake_uu, monkeypatch, capsys):
    monkeypatch.setenv("BRIGHTSPACE_PASSPHRASE", "long enough passphrase")
    save_creds({"solisid": SOLISID, "password": PASSWORD, "totp": TOTP_SECRET},
               "long enough passphrase")

    code, _, _ = run(["login"], capsys)

    assert code == 0
    assert fake_uu.logins == 1


def test_courses_uses_cached_token_without_login(fake_uu, capsys):
    api.save_token(fake_uu.token)

    code, out, _ = run(["courses"], capsys)

    assert code == 0
    assert "INFOMAI" in out and "Fake Course" in out
    assert fake_uu.logins == 0


def test_expired_token_asks_to_log_in_again(fake_uu, capsys):
    api.save_token(make_jwt(time.time() - 600))

    code, _, _ = run(["courses"], capsys)

    assert code == api.EXPIRED_MSG
    assert fake_uu.logins == 0


@pytest.mark.parametrize("command, expected", [
    ("assignments", ["Essay 1", "2026-11-01"]),
    ("deadlines", ["2026-10-01", "Essay 1 (opens)", "2026-11-01"]),
    ("news", ["Room change", "Ruppert 040"]),
    ("grades", ["Essay 1", "7.5/10"]),
])
def test_course_commands(fake_uu, capsys, command, expected):
    api.save_token(fake_uu.token)

    code, out, _ = run([command, "--org-unit", ORG_UNIT], capsys)

    assert code == 0
    for text in expected:
        assert text in out


def test_sync_downloads_files_pages_and_linked_files(fake_uu, secret_env, state, capsys):
    out_dir = state / "mirror"

    code, _, err = run(["sync", "--org-unit", ORG_UNIT, "-o", str(out_dir)], capsys)

    course = out_dir / ORG_UNIT
    assert code == 0, err
    assert (course / "Week 1" / "Slides.pdf").read_bytes() == PDF
    assert "handout" in (course / "Week 1" / "Reading.html").read_text()
    assert "Welcome to the course." in (course / "Week 1" / "Welcome.html").read_text()
    assert "URL=https://example.org/course" in (course / "Week 1" / "Course site.url").read_text()
    assert json.loads((course / "_assignments" / "Essay 1.json").read_text())["due_date"].startswith("2026-11-01")
    assert (course / "_assignments" / "brief.pdf").read_bytes() == PDF
    assert (course / "_linked_files" / "Reading" / "handout.pdf").read_bytes() == PDF
    assert (course / "Lectures" / "Lecture 1.pdf").read_bytes() == PDF
    assert (course / "Lectures" / "_description.txt").read_text() == "Lecture 1 slides"
    # A fresh browser has lost the session cookies, so sync logs in by itself.
    assert fake_uu.logins == 1


def test_sync_all_dry_run_lists_only_course_offerings(fake_uu, capsys):
    api.save_token(fake_uu.token)

    code, out, _ = run(["sync-all", "--dry-run"], capsys)

    assert code == 0
    assert "INFOMAI" in out and "GRP-1" not in out
    assert "1 course(s) would be synced." in out
