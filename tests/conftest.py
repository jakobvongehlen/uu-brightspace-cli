"""Shared fixtures: isolated state, and the offline fake wired into every browser the CLI opens."""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_uu import FakeUU
from uu_cli import auth, paths

LOGIN_ENV = [
    "BRIGHTSPACE_SOLISID", "BRIGHTSPACE_PASSWORD", "BRIGHTSPACE_TOTP_SECRET",
    "BRIGHTSPACE_PASSPHRASE", "BRIGHTSPACE_ORG_UNIT", "BRIGHTSPACE_AUTH",
    "ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN",
]


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch) -> Path:
    """Keep every state file in tmp_path, clear login variables, and act as an unattended run."""
    for var in LOGIN_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "session")
    monkeypatch.setattr(paths, "TOKEN_FILE", tmp_path / "token.json")
    monkeypatch.setattr(paths, "CREDS_FILE", tmp_path / "credentials.enc")
    monkeypatch.setattr(paths, "OUTPUT_DIR", tmp_path / "out")
    return tmp_path


def _chromium_installed() -> bool:
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:
        return False


@pytest.fixture
def fake_uu(monkeypatch) -> FakeUU:
    """Serve the fake UU login and Brightspace to every browser context the CLI launches."""
    if not _chromium_installed():
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    from playwright.sync_api import BrowserType, Page

    fake = FakeUU()
    launch = BrowserType.launch_persistent_context

    def launch_with_fake(self, user_data_dir, **kwargs):
        ctx = launch(self, user_data_dir, **kwargs)
        fake.install(ctx)
        return ctx

    monkeypatch.setattr(BrowserType, "launch_persistent_context", launch_with_fake)
    # The fake answers at once, so the CLI's fixed waits only slow the suite down. Keep enough
    # time for the fake's in-page redirects to land, as the real ones do within the full wait.
    wait = Page.wait_for_timeout
    monkeypatch.setattr(Page, "wait_for_timeout", lambda self, timeout: wait(self, min(timeout, 1500)))
    return fake


@pytest.fixture
def window(request, fake_uu, monkeypatch):
    """The window --auth browser and `uu library open` open, headless here; returns the windows opened.

    By default you fill in UU's forms in it (and click "View PDF"); parametrize with False for a
    window nobody uses.
    """
    from uu_cli import browser

    by_hand = getattr(request, "param", True)
    opened = []
    real_open_window = browser.open_window

    def open_window(ctx):
        win = real_open_window(ctx, headless=True)
        fake_uu.install(win, by_hand=by_hand)
        opened.append(win)
        return win

    monkeypatch.setattr(browser, "open_window", open_window)
    monkeypatch.setenv("DISPLAY", ":0")
    return opened
