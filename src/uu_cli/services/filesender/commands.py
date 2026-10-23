"""SURFfilesender commands: login, send, transfers."""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import BrowserContext

from uu_cli import auth
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


# ── login: the API details from your profile page ─────────────────────────────

def fetch_account(ctx: BrowserContext) -> api.Account | None:
    """Your username and API secret, from the profile page's configuration download.

    Opening the profile page first makes the secret when you have none yet.
    """
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(api.PROFILE, wait_until="load", timeout=30_000)
    if urlparse(page.url).hostname != urlparse(api.BASE_URL).hostname:
        return None
    if accept_service_terms(page):
        page.goto(api.PROFILE, wait_until="load", timeout=30_000)
    text = page.evaluate("""async (url) => {
        const r = await fetch(url, {credentials: "same-origin"});
        return r.ok ? await r.text() : "";
    }""", api.ACCOUNT_CONFIG)
    return api.parse_account_config(text)


def accept_service_terms(page) -> bool:
    """Accept the terms page SURFfilesender shows once before anything else; True when it was there.

    It stands in front of every page, the profile with the API secret included, so the CLI
    can't go on without it. It says what it accepted.
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


def ensure_account(ctx: BrowserContext, args) -> api.Account | None:
    """Log in unless already in (an existing UU session needs no code), then read the API details."""
    account = fetch_account(ctx)
    if account:
        return account
    print("[AUTH] Logging in to SURFfilesender...", file=sys.stderr)
    if single_sign_on(ctx, api.LOGIN) and (account := fetch_account(ctx)):
        print("[AUTH] ✓ Logged in with your existing UU session.", file=sys.stderr)
        return account
    if not auth.ready_to_login(args):
        print(f"ERROR: Cannot log in: {auth.CANNOT_LOGIN}.", file=sys.stderr)
        return None
    log_in(ctx, api.LOGIN, args, name="SURFfilesender")
    account = fetch_account(ctx)
    if not account:
        print(f"ERROR: Logged in, but your profile has no API secret. Open {api.PROFILE}, "
              "make one under \"Remote authentication\", and run this again.", file=sys.stderr)
    return account


def cmd_login(ctx: BrowserContext, args) -> int:
    account = ensure_account(ctx, args)
    if not account:
        return 1
    path = api.save_account(account)
    print(f"Logged in to SURFfilesender; your API details are in {path} (only you can read it).",
          file=sys.stderr)
    return 0


# ── send and list ──────────────────────────────────────────────────────────────

def _account() -> api.Account | None:
    account = api.load_account()
    if not account:
        print("ERROR: No SURFfilesender API details yet: run `uu filesender login` first.",
              file=sys.stderr)
    return account


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
    account = _account()
    if not account:
        return 1
    password = None
    if args.encrypt:
        password = _password()
        if not password:
            return 1
    else:
        print(UNENCRYPTED, file=sys.stderr)

    try:
        transfer = api.send(account, files, recipients, subject=args.subject, message=args.message,
                            days=args.days, password=password,
                            progress=lambda text: print(f"[FILESENDER] {text}", file=sys.stderr))
    except api.FilesenderError as err:
        print(f"ERROR: SURFfilesender: {err}", file=sys.stderr)
        return 1
    except requests.RequestException as err:  # no network, a proxy, TLS
        print(f"ERROR: Could not reach SURFfilesender: {err}", file=sys.stderr)
        return 1
    print(f"Sent {len(files)} file{'s' if len(files) != 1 else ''} to {', '.join(recipients)}; "
          f"available until {_date(transfer.get('expires'))}.")
    if password:
        print("Give the recipients the password another way (a call, a text message), not by email.",
              file=sys.stderr)
    return 0


def cmd_transfers(args) -> int:
    account = _account()
    if not account:
        return 1
    try:
        rows = api.transfers(account)
    except api.FilesenderError as err:
        print(f"ERROR: SURFfilesender: {err}", file=sys.stderr)
        return 1
    except requests.RequestException as err:
        print(f"ERROR: Could not reach SURFfilesender: {err}", file=sys.stderr)
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
