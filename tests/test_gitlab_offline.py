"""UU's GitLab end to end, in a real Chromium, against the offline fake."""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_uu import PASSWORD, SOLISID
from uu_cli import auth
from uu_cli.services.gitlab import commands
from uu_cli.services.gitlab.cli import main as gitlab

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTheTestsOnly student@laptop"


def run(main, argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out, err = capsys.readouterr()
    return exc.value.code, out, err


@pytest.fixture
def creds(monkeypatch):
    """Your Solis-id and password in the environment, as save-creds or the variables give them."""
    monkeypatch.setenv("UU_SOLISID", SOLISID)
    monkeypatch.setenv("UU_PASSWORD", PASSWORD)


# ── reading your public key ───────────────────────────────────────────────────

def test_public_key_reads_one_public_key(tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text(KEY + "\n")

    assert commands.public_key(pub) == KEY


def test_public_key_never_opens_a_file_that_is_not_pub(tmp_path, monkeypatch):
    private = tmp_path / "id_ed25519"
    private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA\n")
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: pytest.fail("opened the file"))

    with pytest.raises(ValueError, match="not a .pub file"):
        commands.public_key(private)


@pytest.mark.parametrize("content, error", [
    (None, "ssh-keygen -t ed25519"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA\n", "doesn't hold one public SSH key"),
    (f"{KEY}\n{KEY}\n", "doesn't hold one public SSH key"),
])
def test_public_key_says_what_is_wrong(tmp_path, content, error):
    pub = tmp_path / "id_ed25519.pub"
    if content is not None:
        pub.write_text(content)

    with pytest.raises(ValueError, match=error):
        commands.public_key(pub)


# ── logging in ────────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_login_fills_in_gitlabs_solis_id_form_without_a_code(fake_uu, creds, capsys):
    code, _, err = run(gitlab, ["login"], capsys)

    assert code == 0, err
    assert "Logged in to GitLab as @student (Test Student)" in err
    assert fake_uu.gitlab_logins == 1
    assert fake_uu.codes == []  # GitLab's own form: UU's code step never came up


@pytest.mark.browser
def test_login_asks_only_for_solis_id_and_password_at_the_terminal(fake_uu, monkeypatch, capsys):
    prompts: list[str] = []
    monkeypatch.setattr(auth, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or SOLISID)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or PASSWORD)

    code, _, err = run(gitlab, ["login"], capsys)

    assert code == 0, err
    assert prompts == ["Solis-id: ", "Password: "]


@pytest.mark.browser
def test_a_wrong_password_says_what_gitlab_said(fake_uu, creds, monkeypatch, capsys):
    monkeypatch.setenv("UU_PASSWORD", "wrong")

    code, _, err = run(gitlab, ["login"], capsys)

    assert code == 1
    assert "GitLab refused the login" in err and "Invalid credentials" in err
    assert fake_uu.gitlab_logins == 0


@pytest.mark.browser
def test_gitlabs_own_two_factor_step_sends_you_to_the_window(fake_uu, creds, capsys):
    fake_uu.gitlab_two_factor = True

    code, _, err = run(gitlab, ["login"], capsys)

    assert code == 1
    assert "its own two-factor code" in err and "--auth browser" in err


@pytest.mark.browser
def test_without_a_way_to_log_in_says_why(fake_uu, capsys):
    code, _, err = run(gitlab, ["projects"], capsys)

    assert code == 1
    assert "needs your Solis-id and password" in err
    assert fake_uu.gitlab_logins == 0


@pytest.mark.browser
def test_you_log_in_in_the_window_and_the_cli_takes_over(window, fake_uu, capsys):
    code, _, err = run(gitlab, ["login", "--auth", "browser"], capsys)

    assert code == 0, err
    assert len(window) == 1 and "Log in to GitLab in the browser window" in err
    assert fake_uu.gitlab_logins == 1
    assert not window[0].browser.is_connected()


@pytest.mark.browser
@pytest.mark.parametrize("window", [False], indirect=True)
def test_window_gives_up_and_closes_when_nobody_logs_in(window, fake_uu, monkeypatch, capsys):
    monkeypatch.setattr(commands, "WINDOW_TIMEOUT_S", 1)

    code, _, err = run(gitlab, ["login", "--auth", "browser"], capsys)

    assert code == 1 and "No login in the browser window" in err
    assert len(window) == 1 and not window[0].browser.is_connected()


@pytest.mark.browser
def test_the_session_is_kept_for_the_next_run(fake_uu, creds, monkeypatch, capsys):
    run(gitlab, ["login"], capsys)
    monkeypatch.delenv("UU_PASSWORD")

    code, out, err = run(gitlab, ["projects"], capsys)

    assert code == 0, err
    assert "No projects yet" in out
    assert fake_uu.gitlab_logins == 1


@pytest.mark.browser
def test_gitlab_pages_load_only_gitlabs_own_files(fake_uu, creds, capsys):
    code, _, err = run(gitlab, ["login"], capsys)

    assert code == 0, err
    assert fake_uu.elsewhere == []  # the avatar from gravatar.com never left the browser


# ── projects and keys ─────────────────────────────────────────────────────────

@pytest.mark.browser
def test_create_makes_an_empty_private_project(fake_uu, creds, capsys):
    # One run: logged out at the start, so the API call needs the token of the new session.
    code, out, err = run(gitlab, ["create", "thesis"], capsys)

    assert code == 0, err
    assert fake_uu.gitlab_created == [
        {"name": "thesis", "visibility": "private", "initialize_with_readme": False}]
    assert "Created https://git.science.uu.nl/student/thesis (private)." in out
    assert "git remote add origin git@git.science.uu.nl:student/thesis.git" in out


@pytest.mark.browser
def test_create_right_after_logging_in_in_the_window(window, fake_uu, capsys):
    # The CLI's own page was loaded as a guest; its CSRF token died with the login in the window.
    code, out, err = run(gitlab, ["create", "thesis", "--auth", "browser"], capsys)

    assert code == 0, err
    assert len(window) == 1 and len(fake_uu.gitlab_created) == 1


@pytest.mark.browser
def test_create_says_when_the_name_is_taken(fake_uu, creds, capsys):
    run(gitlab, ["create", "thesis"], capsys)

    code, _, err = run(gitlab, ["create", "thesis"], capsys)

    assert code == 1
    assert "path has already been taken" in err
    assert len(fake_uu.gitlab_projects) == 1


@pytest.mark.browser
def test_projects_lists_yours_with_their_ssh_address(fake_uu, creds, capsys):
    run(gitlab, ["create", "thesis", "--description", "Notes and code"], capsys)

    code, out, err = run(gitlab, ["projects"], capsys)

    assert code == 0, err
    assert "private   student/thesis" in out
    assert "git@git.science.uu.nl:student/thesis.git" in out


@pytest.mark.browser
def test_add_key_adds_your_public_key(fake_uu, creds, state, capsys):
    pub = state / "id_ed25519.pub"
    pub.write_text(KEY + "\n")

    code, out, err = run(gitlab, ["add-key", str(pub), "--title", "laptop"], capsys)

    assert code == 0, err
    assert fake_uu.gitlab_keys == [{"title": "laptop", "key": KEY}]
    assert "ssh -T git@git.science.uu.nl" in out


@pytest.mark.browser
def test_add_key_sends_nothing_for_a_private_key(fake_uu, creds, state, capsys):
    private = state / "id_ed25519"
    private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA\n")

    code, _, err = run(gitlab, ["add-key", str(private)], capsys)

    assert code == 1 and "not a .pub file" in err
    assert fake_uu.gitlab_api == [] and fake_uu.gitlab_logins == 0
