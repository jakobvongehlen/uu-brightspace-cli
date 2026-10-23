"""Log in through UU's Solis-id page: the NetIQ form, then the one-time code.

Services reach that page their own way: Brightspace redirects straight to it, Konjoin goes
through SURFconext, which may first ask which institution to use. One UU login also covers
the next service while UU's session lasts (single sign-on).

The CLI fills the form itself (do_login), or, with --auth browser, opens a browser window
where you log in yourself (browser_login).
"""

from __future__ import annotations

import re
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page

from uu_cli import auth, browser, paths
from uu_cli.auth import OtpRoute

# How UU appears in SURFconext's institution picker (English or Dutch); not "UMC Utrecht".
UU_IN_SURFCONEXT = re.compile(r"Utrecht University|Universiteit Utrecht")

# How long the --auth browser window waits for you to finish logging in.
WINDOW_TIMEOUT_S = 300


def log_in(ctx: BrowserContext, start_url: str, args, name: str = "Brightspace") -> None:
    """Log in the way args.auth says (after auth.ready_to_login): in a window, or here."""
    if args.auth == "browser":
        browser_login(ctx, start_url, name)
    else:
        do_login(ctx, start_url, args.solisid, args.password, auth.otp_route(args), name=name)


def single_sign_on(ctx: BrowserContext, start_url: str) -> bool:
    """Open start_url; True when an existing UU session logs in without showing the form."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(start_url, wait_until="load", timeout=30_000)
    return not _reach_login_form(page, urlparse(start_url).hostname)


def do_login(ctx: BrowserContext, start_url: str, solisid: str, password: str,
             otp: OtpRoute, name: str = "Brightspace") -> None:
    """Open start_url, which redirects to the Solis-id form, and log in until back on its host.

    The one-time code comes from otp (see uu_cli.auth), asked for once the code form is up.
    """
    if not solisid:
        print("ERROR: --solisid or BRIGHTSPACE_SOLISID required for login",
              file=sys.stderr)
        sys.exit(1)

    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    print(f"[AUTH] Navigating to {name}...", file=sys.stderr)
    page.goto(start_url, wait_until="load", timeout=30_000)
    if not _reach_login_form(page, urlparse(start_url).hostname):
        print(f"[AUTH] ✓ Already logged in at UU: {page.url}", file=sys.stderr)
        return
    print(f"[AUTH] At NISP form: {page.url}", file=sys.stderr)

    print("[AUTH] Filling credentials...", file=sys.stderr)
    page.fill("#Ecom_User_ID", solisid)
    page.fill("#Ecom_Password", password)
    page.click("#loginButton2")

    print("[AUTH] Waiting for OTP form...", file=sys.stderr)
    target = urlparse(start_url).hostname
    otp_sel = _after_password(page, target)
    print(f"[AUTH] URL: {page.url}", file=sys.stderr)
    if otp_sel is None:  # UU asks no code for some services
        print(f"[AUTH] ✓ Logged in without a code: {page.url}", file=sys.stderr)
        page.wait_for_timeout(2000)
        return

    print(f"[AUTH] OTP field found: {otp_sel}", file=sys.stderr)

    code = otp.code()
    page.fill(otp_sel, code)

    button_selectors = ["#loginButton2", "button[type='submit']",
                        "input[type='submit']", "button"]
    btn_sel = None
    for sel in button_selectors:
        if page.locator(sel).count() > 0:
            btn_sel = sel
            break

    if btn_sel:
        page.click(btn_sel)
    else:
        page.keyboard.press("Enter")

    print(f"[AUTH] Waiting for {name}...", file=sys.stderr)
    if not _wait_for_service(page, target):
        _dump(page, f"debug_not_{name.lower()}")
        body_text = page.inner_text("body")[:300]
        raise RuntimeError(f"Post-OTP URL is not {name}: {page.url}\nBody: {body_text}")

    print(f"[AUTH] ✓ Logged in: {page.url}", file=sys.stderr)
    page.wait_for_timeout(2000)


# The OTP field on UU's code page.
_OTP_FIELD_JS = (
    "() => {"
    "  const el = document.querySelector("
    "    'input[autocomplete=\"one-time-code\"],"
    "    input[autocomplete=\"one-time-password\"],"
    "    input[autocomplete=\"otp\"],"
    "    input[maxlength=\"6\"],"
    "    input[type=\"text\"]'"
    "  );"
    "  if (el && el.id) return '#' + el.id;"
    "  if (el && el.name) return 'input[name=\"' + el.name + '\"]';"
    "  return null;"
    "}"
)


def _after_password(page: Page, target_host: str | None, timeout_s: float = 20) -> str | None:
    """After the password: the OTP field's selector, or None when it went on to target_host without one.

    UU asks no code for some services (edu.nl): then SURFconext's consent page, if any, and
    the service follow straight away.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    on_target_since = None
    while time.monotonic() < deadline:
        try:
            host = urlparse(page.url).hostname or ""
            if host == "login.uu.nl":
                if page.locator("#Ecom_Password").count():
                    if time.monotonic() - start > 5:  # the form came back
                        raise RuntimeError("UU didn't accept the Solis-id or password.")
                else:
                    otp_sel = page.evaluate(_OTP_FIELD_JS)
                    if otp_sel:
                        return otp_sel
            elif host.endswith("surfconext.nl"):
                _give_consent(page)
            if host == target_host:
                on_target_since = on_target_since or time.monotonic()
                if time.monotonic() - on_target_since > 2:
                    return None
            else:
                on_target_since = None
        except PlaywrightError:
            pass  # the page navigated mid-check; look again
        page.wait_for_timeout(250)
    _dump(page, "debug_otp_missing")
    body_text = page.inner_text("body")[:300]
    print(f"[AUTH] OTP not found. Body: {body_text}", file=sys.stderr)
    raise RuntimeError(f"OTP field not found. URL={page.url}")


def _wait_for_service(page: Page, target_host: str | None, timeout_s: float = 20) -> bool:
    """After the code: wait until back on target_host (for 2 s), past SURFconext's consent page."""
    deadline = time.monotonic() + timeout_s
    on_target_since = None
    while time.monotonic() < deadline:
        try:
            host = urlparse(page.url).hostname or ""
            if host.endswith("surfconext.nl"):
                _give_consent(page)
            if host == target_host:
                on_target_since = on_target_since or time.monotonic()
                if time.monotonic() - on_target_since > 2:
                    return True
            else:
                on_target_since = None
        except PlaywrightError:
            pass  # the page navigated mid-check; look again
        page.wait_for_timeout(250)
    return False


def _give_consent(page: Page) -> None:
    """Answer SURFconext's one-time "Review your information that will be shared" page.

    It comes the first time you log in to a service through SURFconext. Logging in to that
    service is what you asked for, so the CLI says what the service gets and proceeds.
    """
    accept = page.locator("form#accept button[type=submit]")
    if not accept.count():
        return
    shared = page.locator("form#accept").evaluate(
        "f => (f.closest('main') || document.body).innerText")
    lines = [ln.strip() for ln in shared.splitlines() if ln.strip()]
    print("[AUTH] SURFconext asks once for your consent to share this with the service:",
          file=sys.stderr)
    for ln in lines[:12]:
        print(f"[AUTH]   {ln}", file=sys.stderr)
    print("[AUTH] SURFconext: giving consent", file=sys.stderr)
    accept.first.click()
    page.wait_for_load_state("load")


def browser_login(ctx: BrowserContext, start_url: str, name: str = "Brightspace") -> None:
    """Log in yourself in a visible browser window; ctx takes its session over afterwards.

    It is UU's own login page in an ordinary browser, so whatever UU offers should work there
    (tried with a code from an authenticator app, not yet with push), and the CLI never sees
    your password or code. The window only opens when UU's session has run out.
    """
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    target = urlparse(start_url).hostname

    print(f"[AUTH] Navigating to {name}...", file=sys.stderr)
    page.goto(start_url, wait_until="load", timeout=30_000)
    if not _reach_login_form(page, target):
        print(f"[AUTH] ✓ Already logged in at UU: {page.url}", file=sys.stderr)
        return

    print(f"[AUTH] Log in to UU in the browser window that opens; waiting up to "
          f"{WINDOW_TIMEOUT_S // 60} minutes...", file=sys.stderr)
    window = browser.open_window(ctx)
    try:
        win_page = window.new_page()
        win_page.goto(start_url, wait_until="load", timeout=30_000)
        try:
            _reach_login_form(win_page, target)  # answer SURFconext's picker for you
        except RuntimeError:
            pass  # no form we know; you can still log in by hand
        landed = _wait_until_back(win_page, target, WINDOW_TIMEOUT_S)
        ctx.add_cookies(window.cookies())
    finally:
        window.browser.close()

    page.goto(landed, wait_until="load", timeout=30_000)
    print(f"[AUTH] ✓ Logged in: {page.url}", file=sys.stderr)


def _wait_until_back(page: Page, target_host: str | None, timeout_s: float) -> str:
    """Wait until a login in the window ends back on target_host (for 2 s); returns that URL."""
    deadline = time.monotonic() + timeout_s
    on_target_since = None
    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError("The browser window was closed before the login finished.")
        if urlparse(page.url).hostname == target_host:
            on_target_since = on_target_since or time.monotonic()
            if time.monotonic() - on_target_since > 2:
                return page.url
        else:
            on_target_since = None
        try:
            page.wait_for_timeout(250)
        except PlaywrightError:
            pass  # closed meanwhile; the next round says so
    raise RuntimeError(f"No login in the browser window within {timeout_s / 60:g} minutes; "
                       f"it was at {page.url}")


def _reach_login_form(page: Page, target_host: str | None, timeout_s: float = 30) -> bool:
    """Follow a service's redirects to the Solis-id form; False when they end on target_host instead.

    Picks UU when SURFconext asks for an institution. Ending up (and staying) back on the
    service without a form means UU's session logged us in already.
    """
    deadline = time.monotonic() + timeout_s
    picked_on = None
    on_target_since = None
    while time.monotonic() < deadline:
        try:
            if page.locator("#Ecom_User_ID").count():
                return True
            host = urlparse(page.url).hostname or ""
            if host.endswith("surfconext.nl"):
                _give_consent(page)  # still logged in at UU, but new to this service
            if host.endswith("surfconext.nl") and page.url != picked_on:
                uu = page.get_by_text(UU_IN_SURFCONEXT).first
                if uu.count():
                    print("[AUTH] SURFconext: choosing Utrecht University", file=sys.stderr)
                    picked_on = page.url
                    uu.click()
            if host == target_host:
                on_target_since = on_target_since or time.monotonic()
                if time.monotonic() - on_target_since > 2:
                    return False
            else:
                on_target_since = None
        except PlaywrightError:
            pass  # the page navigated mid-check; look again
        page.wait_for_timeout(250)
    _dump(page, "debug_login_form_missing")
    raise RuntimeError(f"UU login form not found. URL={page.url}")


def _dump(page: Page, name: str) -> None:
    """Save a screenshot and the HTML of a page that broke the login, for debugging."""
    page.screenshot(path=str(paths.SESSION_DIR / f"{name}.png"))
    with open(str(paths.SESSION_DIR / f"{name}.html"), "w") as f:
        f.write(page.content())
