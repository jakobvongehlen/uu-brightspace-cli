"""What every service shares: `uu login`, `uu save-creds`, the variables and the state folder."""

from __future__ import annotations

import json

import pyotp
import pytest

from fake_uu import PASSWORD, SOLISID, TOTP_SECRET
from uu_cli import auth, env, paths
from uu_cli.auth.credentials import load_creds
from uu_cli.cli import main as uu
from uu_cli.services.brightspace.cli import main as brightspace


def run(main, argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out, err = capsys.readouterr()
    return exc.value.code, out, err


@pytest.fixture
def at_terminal(monkeypatch):
    """Someone at the terminal reading codes off their phone; returns the prompts they saw."""
    prompts: list[str] = []

    def answer(prompt=""):
        prompts.append(prompt)
        return SOLISID if prompt.startswith("Solis-id") else pyotp.TOTP(TOTP_SECRET).now()

    monkeypatch.setattr(auth, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or PASSWORD)
    return prompts


# ── uu login ──────────────────────────────────────────────────────────────────
@pytest.mark.browser
def test_login_to_several_services_asks_once(fake_uu, at_terminal, capsys):
    code, _, err = run(uu, ["login", "brightspace", "konjoin", "edunl"], capsys)

    assert code == 0, err
    assert at_terminal == ["Solis-id: ", "Password: ", "Code from your authenticator app: "]
    assert fake_uu.logins == 1 and fake_uu.konjoin_logins == 1 and fake_uu.edunl_logins == 1
    assert json.loads(paths.TOKEN_FILE.read_text())["token"] == fake_uu.token
    assert "Logged in to Konjoin as" in err and "Logged in to edu.nl." in err


@pytest.mark.browser
def test_login_says_which_services_it_could_not_log_in_to(fake_uu, capsys):
    code, _, err = run(uu, ["login", "konjoin"], capsys)

    assert code == 1
    assert "Cannot log in" in err and "Not logged in to konjoin." in err


def test_login_needs_a_service(capsys):
    code, _, err = run(uu, ["login"], capsys)

    assert code == 2 and "required: service" in err


def test_the_library_has_no_login_of_its_own(capsys):
    code, _, err = run(uu, ["login", "library"], capsys)

    assert code == 2 and "invalid choice" in err


# ── uu save-creds ─────────────────────────────────────────────────────────────
def test_save_creds_for_every_service(monkeypatch):
    monkeypatch.setenv("UU_PASSPHRASE", "long enough passphrase")

    with pytest.raises(SystemExit) as exc:
        uu(["save-creds", "--solisid", SOLISID, "--password", PASSWORD, "--totp", TOTP_SECRET])

    assert exc.value.code == 0
    assert load_creds() == {"solisid": SOLISID, "password": PASSWORD, "totp": TOTP_SECRET}


def test_brightspace_save_creds_still_works(monkeypatch, capsys):
    monkeypatch.setenv("UU_PASSPHRASE", "long enough passphrase")

    code, _, _ = run(brightspace, ["save-creds", "--solisid", SOLISID, "--password", PASSWORD,
                                   "--totp", TOTP_SECRET], capsys)

    assert code == 0 and load_creds()["solisid"] == SOLISID


@pytest.mark.parametrize("service", ["konjoin", "gitlab", "remindo"])
def test_every_service_refuses_a_code_for_the_totp_secret(service, capsys):
    code, _, err = run(uu, [service, "login", "--totp", "123456"], capsys)

    assert code == 2 and "expects your TOTP secret key" in err


# ── variables ─────────────────────────────────────────────────────────────────
def test_old_variable_names_still_work(monkeypatch):
    monkeypatch.setenv("BRIGHTSPACE_SOLISID", "old")

    assert env.get("UU_SOLISID") == "old"


def test_new_variable_names_come_first(monkeypatch):
    monkeypatch.setenv("BRIGHTSPACE_SOLISID", "old")
    monkeypatch.setenv("UU_SOLISID", "new")

    assert env.get("UU_SOLISID") == "new"


# ── the state folder ──────────────────────────────────────────────────────────
@pytest.fixture
def default_state(state, monkeypatch):
    """The state in its default place (no UU_STATE_DIR), with the old folder next to it."""
    new = state / "uu-state"
    for name, path in [("STATE_DIR", new), ("DEFAULT_STATE_DIR", new),
                       ("SESSION_DIR", new / "session"), ("TOKEN_FILE", new / "token.json")]:
        monkeypatch.setattr(paths, name, path)
    old = paths.OLD_STATE_DIR
    (old / "session").mkdir(parents=True)
    (old / "credentials.enc").write_text("{}")
    return old, new


def test_the_old_state_folder_moves_over_once(default_state, capsys):
    old, new = default_state

    paths.ensure_state_dirs()

    assert not old.exists() and (new / "credentials.enc").read_text() == "{}"
    assert f"from {old} to {new}" in capsys.readouterr().err


def test_an_existing_state_folder_stays_as_it_is(default_state):
    old, new = default_state
    new.mkdir()

    paths.ensure_state_dirs()

    assert (old / "credentials.enc").exists() and not (new / "credentials.enc").exists()


def test_a_state_folder_of_your_own_leaves_the_old_one_alone(default_state, monkeypatch, tmp_path):
    old, _ = default_state
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "mine")

    paths.ensure_state_dirs()

    assert (old / "credentials.enc").exists()
