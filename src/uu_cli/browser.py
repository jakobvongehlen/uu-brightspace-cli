"""The headless Chromium that every command runs in."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import ssl
import time
import warnings
from pathlib import Path
from urllib.parse import unquote, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from playwright.sync_api import (
    BrowserContext, BrowserType, Error as PlaywrightError, Playwright, Route,
)

from uu_cli import paths

# Session cookies kept between runs, inside the (owner-only) session dir.
COOKIES_FILE = "cookies.json"

CHROMIUM_ARGS = [
    "--no-sandbox",
    # ── Single-process & memory reduction ──
    "--single-process",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-dev-shm-usage",
    # ── Strip non-essential features ──
    "--disable-extensions",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-features=TranslateUI,BackForwardCache",
    "--no-first-run",
]

# The visible window of --auth browser: an ordinary Chromium, so UU's pages behave as they do for you.
WINDOW_ARGS = ["--no-first-run", "--disable-sync", "--disable-default-apps", "--disable-extensions"]

# The browser type behind each context launch() opened. Older Playwright versions (the package
# allows 1.40 and up) give a persistent context no .browser, so open_window can't count on it.
_launched_by: dict[BrowserContext, BrowserType] = {}


def launch(pw: Playwright, session_dir: str | Path) -> BrowserContext:
    """Open the persistent browser profile in session_dir, with the last run's session restored."""
    proxy, trust = network()
    ctx = pw.chromium.launch_persistent_context(
        str(session_dir),
        headless=True,
        no_viewport=True,
        args=CHROMIUM_ARGS + trust,
        proxy=proxy,
    )
    _launched_by[ctx] = pw.chromium
    restore_session(ctx, session_dir)
    return ctx


def network() -> tuple[dict | None, list[str]]:
    """Chromium's proxy (from HTTPS_PROXY) and the flag that trusts the CAs SSL_CERT_FILE adds.

    curl and Python follow both variables by themselves; Chromium follows neither. Behind a proxy
    that terminates TLS (a company network, for one) every site comes re-signed by the proxy's
    own CA, which only SSL_CERT_FILE names.
    """
    proxy = None
    url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if url:
        p = urlparse(url)
        proxy = {"server": f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "")}
        if p.username:
            proxy["username"] = unquote(p.username)
            proxy["password"] = unquote(p.password or "")
        if bypass := os.environ.get("NO_PROXY") or os.environ.get("no_proxy"):
            proxy["bypass"] = bypass
    added = _added_cas()
    trust = [f"--ignore-certificate-errors-spki-list={','.join(sorted(added))}"] if added else []
    return proxy, trust


def _added_cas() -> set[str]:
    """The SPKI hashes of the CAs in SSL_CERT_FILE that the system's bundle doesn't have.

    Chromium skips every certificate check for a chain holding one of these keys, so only the
    added CAs go in, never the public ones; when the system's bundle can't be read, none do.
    """
    added = os.environ.get("SSL_CERT_FILE")
    system = ssl.get_default_verify_paths().openssl_cafile
    if not added or not system:
        return set()
    try:
        return _spki_hashes(Path(added)) - _spki_hashes(Path(system))
    except OSError:
        return set()


def _spki_hashes(pem_file: Path) -> set[str]:
    hashes = set()
    for pem in re.findall(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----",
                          pem_file.read_bytes(), re.S):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # old roots with a negative serial number
                key = x509.load_pem_x509_certificate(pem).public_key()
        except ValueError:
            continue
        der = key.public_bytes(serialization.Encoding.DER,
                               serialization.PublicFormat.SubjectPublicKeyInfo)
        hashes.add(base64.b64encode(hashlib.sha256(der).digest()).decode())
    return hashes


def open_window(ctx: BrowserContext, headless: bool = False) -> BrowserContext:
    """A visible browser window that starts with ctx's cookies, to log in by hand.

    Close it with `.browser.close()`; ctx.add_cookies(window.cookies()) takes its session over.
    headless is for the tests.
    """
    browser_type = ctx.browser.browser_type if ctx.browser else _launched_by[ctx]
    proxy, trust = network()
    window = browser_type.launch(headless=headless, args=WINDOW_ARGS + trust, proxy=proxy)
    win = window.new_context(no_viewport=True)
    win.add_cookies(ctx.cookies())
    return win


def own_files_only(ctx: BrowserContext, host: str) -> None:
    """On host's pages, load only host's own files; leaving for another site (a login) still works.

    Konjoin's pages wait for third-party scripts and fonts (cdn.polyfill.io, cdnjs, Google
    Fonts) before they count as loaded. The CLI needs none of them, and when one hangs, the page
    hangs past every timeout. cdn.polyfill.io is also the domain that served malware in 2024; it
    has no business running next to a login session.
    """
    def route(r: Route) -> None:
        req = r.request
        try:
            on_host = urlparse(req.frame.url).hostname == host
        except PlaywrightError:
            on_host = False  # not from a page (a service worker): leave it alone
        if on_host and not req.is_navigation_request() and urlparse(req.url).hostname != host:
            r.abort()
        else:
            r.fallback()

    ctx.route("**/*", route)


def restore_session(ctx: BrowserContext, session_dir: str | Path) -> None:
    """Put back the session cookies save_session kept; Chromium drops them when it closes."""
    try:
        cookies = json.loads((Path(session_dir) / COOKIES_FILE).read_text())
    except (OSError, ValueError):
        return
    now = time.time()
    live = [c for c in cookies if c.get("expires", -1) == -1 or c["expires"] > now]
    if live:
        ctx.add_cookies(live)


def save_session(ctx: BrowserContext, session_dir: str | Path) -> None:
    """Keep this run's session cookies (logins at UU and the platforms) for the next run.

    They are as good as a login until the server ends the session, so the file is owner-only.
    """
    try:
        session = [c for c in ctx.cookies() if c.get("expires", -1) == -1]
        paths.write_private(Path(session_dir) / COOKIES_FILE, json.dumps(session))
    except Exception:
        pass  # best effort: the next run just logs in again
