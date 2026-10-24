"""SURFfilesender commands: login, send, transfers."""

from __future__ import annotations

import getpass
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, sync_playwright

from uu_cli import auth, browser
from uu_cli.login import log_in, single_sign_on
from uu_cli.services.filesender import api

CODE_OF_CONDUCT = ("https://servicedesk.surf.nl/wiki/spaces/WIKI/pages/198967771/"
                   "Huisregels+Code+of+Conduct")

# What the website's upload page makes you tick, every time.
TERMS = ("SURFfilesender asks you to accept its code of conduct for every transfer, as the box on "
         f"its upload page does. Read it ({CODE_OF_CONDUCT}), then add --accept-terms.")
UNENCRYPTED = ("[FILESENDER] Not encrypted: SURF's code of conduct says not to send confidential "
               "content this way without encryption (--encrypt). SURF logs the file names and "
               "recipients for 4 months.")


# ── login: a browser session on SURFfilesender ────────────────────────────────

def accept_service_terms(page) -> bool:
    """Accept the terms page SURFfilesender shows once before anything else; True when it was there.

    It stands in front of every page, the upload page included, so the CLI can't go on
    without it. It says what it accepted.
    """
    accept = page.locator(".serviceaup .service_aup_accept a")
    if not accept.count():
        return False
    title = page.locator(".serviceaup h1").inner_text().strip()
    terms = page.locator(".serviceaup a[target=_blank]").first
    text = terms.inner_text().strip() if terms.count() else ""
    link = terms.get_attribute("href") if terms.count() else CODE_OF_CONDUCT
    print(f"[FILESENDER] Accepting \"{title}\" for you, once: {text} ({link})", file=sys.stderr)
    with page.expect_navigation(wait_until="load", timeout=30_000):
        accept.first.click()
    return True


def _browser_transport(ctx: BrowserContext) -> api.Transport:
    """Requests that carry the browser profile's cookies, as the upload page's own do."""
    def send(method: str, url: str, headers: dict, body: bytes | None):
        resp = ctx.request.fetch(url, method=method, headers=headers, data=body,
                                 timeout=api.TIMEOUT_S * 1000, fail_on_status_code=False)
        return resp.status, resp.headers, resp.body()
    return send


def session_from_page(ctx: BrowserContext) -> api.Session | None:
    """The upload page's session, or None when it isn't logged in."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(api.UPLOAD, wait_until="load", timeout=30_000)
    if urlparse(page.url).hostname != urlparse(api.BASE_URL).hostname:
        return None
    if accept_service_terms(page):
        page.goto(api.UPLOAD, wait_until="load", timeout=30_000)
    session = api.Session(
        send=_browser_transport(ctx),
        token=page.locator("body").get_attribute("data-security-token") or "",
        csrf=next((c["value"] for c in ctx.cookies(api.BASE_URL) if c["name"] == "csrfptoken"), ""))
    try:
        api.transfers(session)  # refused without a login
    except api.NotLoggedIn:
        return None
    return session


def open_session(ctx: BrowserContext, args) -> api.Session | None:
    """Log in unless already in (an existing UU session needs no code); the session for the API."""
    session = session_from_page(ctx)
    if session:
        return session
    print("[AUTH] Logging in to SURFfilesender...", file=sys.stderr)
    if single_sign_on(ctx, api.LOGIN) and (session := session_from_page(ctx)):
        print("[AUTH] ✓ Logged in with your existing UU session.", file=sys.stderr)
        return session
    if not auth.ready_to_login(args):
        print(f"ERROR: Cannot log in: {auth.CANNOT_LOGIN}.", file=sys.stderr)
        return None
    log_in(ctx, api.LOGIN, args, name="SURFfilesender")
    session = session_from_page(ctx)
    if not session:
        print("ERROR: Logged in, but SURFfilesender still doesn't see a session.", file=sys.stderr)
    return session


@contextmanager
def connected(args):
    """A browser on SURFfilesender for the length of a command: yields its session, or None."""
    with sync_playwright() as pw:
        ctx = browser.launch(pw, args.session_dir)
        browser.own_files_only(ctx, urlparse(api.BASE_URL).hostname)
        try:
            yield open_session(ctx, args)
        finally:
            browser.save_session(ctx, args.session_dir)


def cmd_login(args) -> int:
    with connected(args) as session:
        if not session:
            return 1
    print("Logged in to SURFfilesender.", file=sys.stderr)
    return 0


# ── send and list ──────────────────────────────────────────────────────────────

def _password() -> str | None:
    """The encryption password: UU_FILESENDER_PASSWORD, or typed twice. Never a flag (ps, history)."""
    password = os.environ.get("UU_FILESENDER_PASSWORD")
    if password:
        return password
    if not auth.interactive():
        print("ERROR: --encrypt needs a password: type it at a terminal, or set "
              "UU_FILESENDER_PASSWORD.", file=sys.stderr)
        return None
    first = getpass.getpass("Password for the recipients: ")
    if first != getpass.getpass("Same password again: "):
        print("ERROR: The two passwords differ.", file=sys.stderr)
        return None
    return first


def cmd_send(args) -> int:
    if not args.accept_terms:
        print(f"ERROR: {TERMS}", file=sys.stderr)
        return 2
    files = [Path(f) for f in args.files]
    for f in files:
        if not f.is_file():
            what = "a folder (zip it first)" if f.is_dir() else "not a file"
            print(f"ERROR: {f}: {what}.", file=sys.stderr)
            return 2
    recipients = [r.strip() for value in args.to for r in value.split(",") if r.strip()]
    if args.days is not None and args.days < 1:
        print("ERROR: --days is at least 1.", file=sys.stderr)
        return 2
    password = None
    if args.encrypt:
        password = _password()
        if not password:
            return 1
    else:
        print(UNENCRYPTED, file=sys.stderr)

    try:
        with connected(args) as session:
            if not session:
                return 1
            transfer = api.send(session, files, recipients, subject=args.subject,
                                message=args.message, days=args.days, password=password,
                                progress=lambda text: print(f"[FILESENDER] {text}", file=sys.stderr))
    except api.FilesenderError as err:
        print(f"ERROR: SURFfilesender: {err}", file=sys.stderr)
        return 1
    except PlaywrightError as err:  # no network, a proxy, TLS
        print(f"ERROR: Could not reach SURFfilesender: {str(err).splitlines()[0]}", file=sys.stderr)
        return 1
    print(f"Sent {len(files)} file{'s' if len(files) != 1 else ''} to {', '.join(recipients)}; "
          f"available until {_date(transfer.get('expires'))}.")
    if password:
        print("Give the recipients the password another way (a call, a text message), not by email.",
              file=sys.stderr)
    return 0


def cmd_transfers(args) -> int:
    try:
        with connected(args) as session:
            if not session:
                return 1
            rows = api.transfers(session)
    except api.FilesenderError as err:
        print(f"ERROR: SURFfilesender: {err}", file=sys.stderr)
        return 1
    except PlaywrightError as err:
        print(f"ERROR: Could not reach SURFfilesender: {str(err).splitlines()[0]}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No transfers.")
        return 0
    for t in rows:
        names = ", ".join(f.get("name", "?") for f in t.get("files") or [])
        to = ", ".join(r.get("email", "?") for r in t.get("recipients") or [])
        print(f"{t.get('id', '?'):>8}  until {_date(t.get('expires'))}  {names}" + (f"  → {to}" if to else ""))
    return 0


def _date(value) -> str:
    """FileSender sends dates as {"raw": <epoch>, "formatted": "<text>"}."""
    if isinstance(value, dict):
        return str(value.get("formatted") or value.get("raw") or "?")
    return str(value or "?")
