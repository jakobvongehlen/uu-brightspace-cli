"""GitLab commands: login, projects, create, add-key.

GitLab signs you in on its own page, not on UU's: its "Solis-ID" tab checks your Solis-id and
password against UU's directory (LDAP). So UU's single sign-on doesn't carry over and there is
no one-time code; --auth browser opens that page in a window for you instead.
"""

from __future__ import annotations

import getpass
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.sync_api import (
    BrowserContext, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout,
)

from uu_cli import auth, browser
from uu_cli.auth.credentials import have_creds
from uu_cli.services.gitlab.api import HOST, SIGN_IN_URL, call, current_user

# The Solis-ID tab of GitLab's sign-in page; the code step GitLab adds when you turned on its
# own two-factor login; where it says why a sign-in failed.
LDAP_FORM = 'form[action$="/users/auth/ldapmain/callback"]'
TWO_FACTOR = "#user_otp_attempt"
FLASH = ".flash-container"

SIGN_IN_TIMEOUT_S = 30
# How long the --auth browser window waits for you to finish logging in.
WINDOW_TIMEOUT_S = 300

CANNOT_LOGIN = ("GitLab's sign-in needs your Solis-id and password (`uu save-creds`, --password or a "
                "terminal to type them in), or a screen for --auth browser")

# The public key types GitLab takes; each line of a .pub file starts with one.
KEY_TYPES = ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-", "sk-ssh-ed25519@openssh.com ", "sk-ecdsa-sha2-")


class LoginFailed(Exception):
    """GitLab didn't log you in; the message says why."""


def ensure_login(ctx: BrowserContext, args) -> dict | None:
    """Log in to GitLab unless already in; returns the user, or None after saying why not."""
    user = current_user(ctx)
    if user:
        return user
    try:
        if args.auth == "browser":
            if not auth.ready_to_login(args):
                return None
            window_login(ctx)
        elif _have_password(args):
            form_login(ctx, args.solisid, args.password)
        else:
            print(f"ERROR: Cannot log in: {CANNOT_LOGIN}.", file=sys.stderr)
            return None
    except LoginFailed as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return None
    user = current_user(ctx)
    if not user:
        print("ERROR: GitLab's sign-in finished, but GitLab still sees a guest.", file=sys.stderr)
    return user


def _have_password(args) -> bool:
    """Solis-id and password from the options, save-creds or the terminal; no code is needed."""
    have_creds(args)
    if not (args.solisid and args.password) and auth.interactive():
        args.solisid = args.solisid or input("Solis-id: ").strip()
        args.password = args.password or getpass.getpass("Password: ")
    return bool(args.solisid and args.password)


def form_login(ctx: BrowserContext, solisid: str, password: str) -> None:
    """Fill in the Solis-ID tab of GitLab's sign-in page and send it."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    print("[AUTH] Logging in to GitLab with your Solis-id...", file=sys.stderr)
    page.goto(SIGN_IN_URL, wait_until="load", timeout=30_000)
    form = page.locator(LDAP_FORM)
    try:
        form.locator('input[name="username"]').fill(solisid)
        form.locator('input[name="password"]').fill(password)  # GitLab's script renders this one
        with page.expect_navigation(timeout=SIGN_IN_TIMEOUT_S * 1000):
            form.locator('button[type="submit"]').click()
    except PlaywrightTimeout:
        raise LoginFailed(f"GitLab's Solis-ID sign-in form didn't work as expected at {page.url}") from None
    _wait_for_sign_in(page)


def _wait_for_sign_in(page: Page) -> None:
    """Wait until the sent form lands on GitLab; raise LoginFailed when it lands back on the form."""
    deadline = time.monotonic() + SIGN_IN_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if page.locator(TWO_FACTOR).count():
                raise LoginFailed("GitLab asks for its own two-factor code; log in with --auth browser")
            if _signed_in(page.url):
                return
            if urlparse(page.url).path == "/users/sign_in":
                flash = page.locator(FLASH)
                why = flash.first.inner_text().strip() if flash.count() else ""
                raise LoginFailed(f"GitLab refused the login: {why or 'wrong Solis-id or password?'}")
        except PlaywrightError:
            pass  # the page navigated mid-check; look again
        page.wait_for_timeout(250)
    raise LoginFailed(f"GitLab's sign-in didn't finish; it was at {page.url}")


def window_login(ctx: BrowserContext) -> None:
    """Log in yourself on GitLab's sign-in page in a browser window; ctx takes its session over."""
    print(f"[AUTH] Log in to GitLab in the browser window that opens; waiting up to "
          f"{WINDOW_TIMEOUT_S // 60} minutes...", file=sys.stderr)
    window = browser.open_window(ctx)
    try:
        page = window.new_page()
        page.goto(SIGN_IN_URL, wait_until="load", timeout=30_000)
        try:
            page.wait_for_url(_signed_in, timeout=WINDOW_TIMEOUT_S * 1000)
        except PlaywrightTimeout:
            raise LoginFailed(f"No login in the browser window within "
                              f"{WINDOW_TIMEOUT_S / 60:g} minutes") from None
        except PlaywrightError:
            raise LoginFailed("The browser window was closed before the login finished") from None
        ctx.add_cookies(window.cookies())
    finally:
        window.browser.close()


def _signed_in(url: str) -> bool:
    """Whether url is GitLab past its sign-in pages (/users/sign_in, /users/auth/…)."""
    parts = urlparse(url)
    return parts.hostname == HOST and not parts.path.startswith("/users/")


def cmd_login(ctx: BrowserContext, args) -> int:
    user = ensure_login(ctx, args)
    if not user:
        return 1
    print(f"Logged in to GitLab as @{user['username']} ({user.get('name', '')}).", file=sys.stderr)
    return 0


def cmd_projects(ctx: BrowserContext, args) -> int:
    """The projects you own or are a member of, most recently active first."""
    if not ensure_login(ctx, args):
        return 1
    query = urlencode({"membership": "true", "order_by": "last_activity_at", "per_page": 100})
    projects = call(ctx, "GET", f"/projects?{query}")
    if not projects:
        print("No projects yet; create one with `uu gitlab create <name>`.")
        return 0
    for p in projects:
        print(f"{p.get('visibility', '?'):<9} {p.get('path_with_namespace', '?')}")
        print(f"{'':<9} {p.get('ssh_url_to_repo', '')}")
    return 0


def cmd_create(ctx: BrowserContext, args) -> int:
    """Create a project with nothing in it, so an existing repo's first push goes in as it is."""
    if not ensure_login(ctx, args):
        return 1
    data = {"name": args.name, "visibility": args.visibility, "initialize_with_readme": False}
    if args.description:
        data["description"] = args.description
    project = call(ctx, "POST", "/projects", data)
    print(f"Created {project['web_url']} ({project['visibility']}).")
    print("To push an existing repo there, run in that repo:")
    print(f"  git remote add origin {project['ssh_url_to_repo']}")
    print("  git push -u origin HEAD")
    return 0


def cmd_add_key(ctx: BrowserContext, args) -> int:
    """Add your public SSH key to your GitLab account, so git can push over SSH."""
    try:
        key = public_key(Path(args.file).expanduser())
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    if not ensure_login(ctx, args):
        return 1
    title = args.title or socket.gethostname()
    call(ctx, "POST", "/user/keys", {"title": title, "key": key})
    print(f"Added the SSH key '{title}' to your GitLab account. Check it with: ssh -T git@{HOST}")
    return 0


def public_key(path: Path) -> str:
    """The public key in a .pub file. Any other file stays unopened, so a private key is never read."""
    if path.suffix != ".pub":
        raise ValueError(f"{path} is not a .pub file: give your public key (like "
                         "~/.ssh/id_ed25519.pub), never the private one")
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as err:
        raise ValueError(f"Can't read {path} ({err.strerror}); make a key with: "
                         "ssh-keygen -t ed25519") from None
    if not key.startswith(KEY_TYPES) or "\n" in key:
        raise ValueError(f"{path} doesn't hold one public SSH key "
                         "(a single line like 'ssh-ed25519 AAAA… you@laptop')")
    return key
