"""edu.nl end to end, in a real Chromium, against the offline fake."""

from __future__ import annotations

import json

import pyotp
import pytest

from fake_uu import EDUNL, PASSWORD, SOLISID, TOTP_SECRET
from uu_cli import auth
from uu_cli.services.brightspace.cli import main as brightspace
from uu_cli.services.edunl.cli import main as edunl


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


def _link(code, url, label=""):
    return {"code": code, "url": url, "label": label, "clicks": 3, "date": "2026-10-20"}


@pytest.mark.browser
def test_shorten_logs_in_through_surfconext_and_prints_only_the_link(fake_uu, at_terminal, capsys):
    code, out, err = run(edunl, ["shorten", "https://git.science.uu.nl/student/thesis"], capsys)

    assert code == 0, err
    assert out == f"{EDUNL}/c0001\n"
    assert "SURFconext: choosing Utrecht University" in err
    assert fake_uu.edunl_logins == 1 and at_terminal[-1] == "Code from your authenticator app: "
    assert fake_uu.edunl_links[0]["url"] == "https://git.science.uu.nl/student/thesis"


@pytest.mark.browser
def test_shorten_with_a_label(fake_uu, at_terminal, capsys):
    code, out, err = run(edunl, ["shorten", "https://example.org/survey", "--label", "survey"], capsys)

    assert code == 0, err
    assert out == f"{EDUNL}/c0001\n" and "Label: survey" in err
    assert fake_uu.edunl_links[0]["label"] == "survey"


@pytest.mark.browser
def test_shorten_says_what_edu_nl_refuses(fake_uu, at_terminal, capsys):
    code, out, err = run(edunl, ["shorten", "https://localhost/x"], capsys)

    assert code == 1 and out == ""
    assert "not quite a correct URL" in err
    assert fake_uu.edunl_links == []


def test_shorten_wants_a_web_address_before_logging_in(capsys):
    code, out, err = run(edunl, ["shorten", "git.science.uu.nl/student/thesis"], capsys)

    assert code == 2 and out == ""
    assert "starts with http:// or https://" in err


@pytest.mark.browser
def test_links_as_json(fake_uu, at_terminal, capsys):
    fake_uu.edunl_links = [_link("c0002", "https://example.org/b", "slides"),
                           _link("c0001", "https://example.org/a")]

    code, out, err = run(edunl, ["links", "--json"], capsys)

    assert code == 0, err
    assert json.loads(out) == [
        {"short": f"{EDUNL}/c0002", "clicks": 3, "date": "2026-10-20", "url": "https://example.org/b",
         "label": "slides"},
        {"short": f"{EDUNL}/c0001", "clicks": 3, "date": "2026-10-20", "url": "https://example.org/a",
         "label": ""},
    ]


@pytest.mark.browser
def test_links_search_and_pages(fake_uu, at_terminal, capsys):
    fake_uu.edunl_links = [_link(f"c000{i}", f"https://example.org/{i}") for i in (3, 2, 1)]

    code, out, err = run(edunl, ["links"], capsys)
    assert code == 0, err
    assert "c0003" in out and "c0002" in out and "c0001" not in out
    assert "More on page 2 (--page 2)" in out

    code, out, _ = run(edunl, ["links", "--page", "2"], capsys)
    assert "c0001" in out and "More on page" not in out

    code, out, _ = run(edunl, ["links", "--search", "org/2", "--json"], capsys)
    assert [r["short"] for r in json.loads(out)] == [f"{EDUNL}/c0002"]


@pytest.mark.browser
def test_one_code_covers_brightspace_and_edunl(fake_uu, at_terminal, monkeypatch, capsys):
    run(brightspace, ["login"], capsys)
    monkeypatch.setattr(auth, "interactive", lambda: False)  # no terminal, no secret

    code, out, err = run(edunl, ["shorten", "https://example.org/a"], capsys)

    assert code == 0, err
    assert "Logged in with your existing UU session." in err
    assert len(fake_uu.codes) == 1 and fake_uu.edunl_logins == 1


@pytest.mark.browser
def test_edunl_session_is_kept_for_the_next_run(fake_uu, at_terminal, monkeypatch, capsys):
    run(edunl, ["login"], capsys)
    monkeypatch.setattr(auth, "interactive", lambda: False)

    code, out, err = run(edunl, ["links"], capsys)

    assert code == 0, err
    assert "No links found." in out
    assert fake_uu.edunl_logins == 1 and len(fake_uu.codes) == 1


@pytest.mark.browser
def test_edunl_pages_load_only_their_own_files(fake_uu, at_terminal, capsys):
    run(edunl, ["shorten", "https://example.org/a"], capsys)

    assert fake_uu.elsewhere == []


@pytest.mark.browser
def test_without_a_way_to_log_in_says_why(fake_uu, capsys):
    code, out, err = run(edunl, ["shorten", "https://example.org/a"], capsys)

    assert code == 1 and out == ""
    assert "a terminal to type the code" in err
    assert fake_uu.edunl_links == []


@pytest.mark.browser
def test_no_code_and_surfconexts_consent_page(fake_uu, at_terminal, capsys):
    """As on edu.nl: UU asks no code, and SURFconext asks once for your consent."""
    fake_uu.asks_code = False
    fake_uu.consent_needed = {"edunl"}

    code, out, err = run(edunl, ["shorten", "https://example.org/x"], capsys)

    assert code == 0, err
    assert out == f"{EDUNL}/c0001\n"
    assert fake_uu.consents == ["edunl"] and "SURFconext: giving consent" in err
    assert "Review your information that will be shared." in err  # says what it agreed to
    assert fake_uu.codes == [] and not any(p.startswith("Code") for p in at_terminal)


@pytest.mark.browser
def test_consent_page_after_the_code(fake_uu, at_terminal, capsys):
    fake_uu.consent_needed = {"edunl"}

    code, out, err = run(edunl, ["shorten", "https://example.org/x"], capsys)

    assert code == 0, err
    assert fake_uu.consents == ["edunl"] and len(fake_uu.codes) == 1


@pytest.mark.browser
def test_a_wrong_password_says_so(fake_uu, at_terminal, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "wrong")

    with pytest.raises(RuntimeError, match="didn't accept the Solis-id or password"):
        edunl(["login"])


@pytest.mark.browser
def test_consent_page_with_an_existing_uu_session(fake_uu, at_terminal, monkeypatch, capsys):
    run(brightspace, ["login"], capsys)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    fake_uu.consent_needed = {"edunl"}

    code, out, err = run(edunl, ["shorten", "https://example.org/a"], capsys)

    assert code == 0, err
    assert "Logged in with your existing UU session." in err
    assert fake_uu.consents == ["edunl"] and len(fake_uu.codes) == 1


@pytest.mark.browser
def test_a_service_that_wants_a_code_while_still_logged_in(fake_uu, at_terminal, capsys):
    """As bookings.uu.nl does (MFA): UU asks only the code, not the password again."""
    run(brightspace, ["login"], capsys)
    fake_uu.step_up = {"edunl"}

    code, out, err = run(edunl, ["shorten", "https://example.org/a"], capsys)

    assert code == 0, err
    assert "wants a code too" in err and len(fake_uu.codes) == 2
