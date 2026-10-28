"""Login routes (TOTP secret or a code you type) and the session kept between runs."""

from __future__ import annotations

import json
import stat
import time
from argparse import Namespace

import pyotp
import pytest

from fake_uu import COURSE_NAME, ORG_UNIT, PASSWORD, PDF, SOLISID, TOTP_SECRET, make_jwt
from uu_cli import auth, paths
from uu_cli.auth import PromptCode, TotpSecret, credentials
from uu_cli.services.brightspace import api
from uu_cli.services.brightspace.cli import main


def run(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out, err = capsys.readouterr()
    return exc.value.code, out, err


@pytest.fixture
def at_terminal(monkeypatch):
    """A person at the terminal who reads codes off their phone; returns the prompts they saw."""
    prompts: list[str] = []

    def answer(prompt=""):
        prompts.append(prompt)
        if prompt.startswith("Solis-id"):
            return SOLISID
        return pyotp.TOTP(TOTP_SECRET).now()  # what the authenticator app shows right now

    monkeypatch.setattr(auth, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or PASSWORD)
    return prompts


# ── choosing a route ──────────────────────────────────────────────────────────
def args(**kw) -> Namespace:
    return Namespace(**{"solisid": SOLISID, "password": PASSWORD, "totp": "", "auth": "auto", **kw})


def test_auto_uses_the_secret_when_there_is_one():
    a = args(totp=TOTP_SECRET)

    assert auth.ready_to_login(a) and a.auth == "totp-secret"
    assert isinstance(auth.otp_route(a), TotpSecret)


def test_auto_asks_for_the_code_at_a_terminal(monkeypatch):
    monkeypatch.setattr(auth, "interactive", lambda: True)
    a = args()

    assert auth.ready_to_login(a) and a.auth == "prompt"
    assert isinstance(auth.otp_route(a), PromptCode)


def test_unattended_without_a_secret_cannot_log_in():
    assert not auth.ready_to_login(args())


def test_prompt_route_refuses_without_a_terminal(capsys):
    assert not auth.ready_to_login(args(auth="prompt"))
    assert "needs a terminal" in capsys.readouterr().err


def test_prompt_asks_again_until_it_gets_six_digits(monkeypatch, capsys):
    answers = iter(["12ab", "123 456"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert PromptCode().code() == "123456"
    assert "not a 6-digit code" in capsys.readouterr().err


def test_save_creds_without_a_secret_leads_to_the_prompt_route(monkeypatch):
    monkeypatch.setenv("BRIGHTSPACE_PASSPHRASE", "long enough passphrase")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")  # Enter at "TOTP secret"
    assert credentials.cmd_save_creds(Namespace(solisid=SOLISID, password=PASSWORD, totp="")) == 0
    monkeypatch.setattr(auth, "interactive", lambda: True)
    a = Namespace(solisid=None, password="", totp="", auth="auto")

    assert auth.ready_to_login(a)
    assert (a.solisid, a.password, a.auth) == (SOLISID, PASSWORD, "prompt")


# ── end to end ────────────────────────────────────────────────────────────────
@pytest.mark.browser
def test_login_by_typing_the_code(fake_uu, at_terminal, capsys):
    code, _, err = run(["login"], capsys)

    assert code == 0, err
    assert at_terminal == ["Solis-id: ", "Password: ", "Code from your authenticator app: "]
    assert fake_uu.logins == 1 and len(fake_uu.codes) == 1
    assert pyotp.TOTP(TOTP_SECRET).verify(fake_uu.codes[0], valid_window=1)
    assert json.loads(paths.TOKEN_FILE.read_text())["token"] == fake_uu.token


@pytest.mark.browser
def test_session_is_kept_owner_only(fake_uu, at_terminal, capsys):
    run(["login"], capsys)

    cookies_file = paths.SESSION_DIR / "cookies.json"
    names = {c["name"] for c in json.loads(cookies_file.read_text())}
    assert {"d2lSessionVal", "IDP_SESSION"} <= names
    assert stat.S_IMODE(cookies_file.stat().st_mode) == 0o600


@pytest.mark.browser
def test_one_code_then_sync_without_logging_in(fake_uu, at_terminal, monkeypatch, state, capsys):
    run(["login"], capsys)
    monkeypatch.setattr(auth, "interactive", lambda: False)  # later, unattended, no secret anywhere

    code, _, err = run(["sync", "--org-unit", ORG_UNIT, "-o", str(state / "mirror")], capsys)

    assert code == 0, err
    assert "Reusing authenticated browser session." in err
    assert (state / "mirror" / COURSE_NAME / "Week 1" / "Slides.pdf").read_bytes() == PDF
    assert fake_uu.logins == 1 and len(fake_uu.codes) == 1


@pytest.mark.browser
def test_expired_token_is_refreshed_from_the_saved_session(fake_uu, at_terminal, monkeypatch, capsys):
    run(["login"], capsys)
    api.save_token(make_jwt(time.time() - 600))
    monkeypatch.setattr(auth, "interactive", lambda: False)

    code, out, err = run(["courses"], capsys)

    assert code == 0, err
    assert "Token refreshed from the saved session." in err
    assert "INFOMAI" in out
    assert fake_uu.logins == 1


@pytest.mark.browser
def test_without_a_session_or_a_way_to_log_in_sync_says_why(fake_uu, state, capsys):
    code, _, err = run(["sync", "--org-unit", ORG_UNIT, "-o", str(state / "mirror")], capsys)

    assert code == 1
    assert "a terminal to type the code" in err
    assert fake_uu.logins == 0
