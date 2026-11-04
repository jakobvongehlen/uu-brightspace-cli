"""How a login answers UU's one-time-code step.

Each route is a module in this package with a class that has a `name` and a `code()` method,
registered in ROUTES:

  totp-secret  generate the code from your TOTP secret (unattended: cron, Docker)
  prompt       ask you for the code from your own authenticator app

`--auth auto` (the default) uses the secret when one is set and asks you otherwise.
`--auth browser` skips the form altogether: you log in yourself in a browser window
(uu_cli.login.browser_login), on UU's own login page.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Protocol

from uu_cli import env
from uu_cli.auth.credentials import have_creds
from uu_cli.auth.prompt import PromptCode
from uu_cli.auth.totp_secret import TotpSecret


class OtpRoute(Protocol):
    name: str

    def code(self) -> str: ...


ROUTES = {route.name: route for route in (TotpSecret, PromptCode)}

CANNOT_LOGIN = ("a login needs --password and --totp (or `uu save-creds`), "
                "a terminal to type the code from your authenticator app, "
                "or a screen for --auth browser")


def add_login_args(parser, d=lambda v: v) -> None:
    """The login options every service shares. d wraps defaults (argparse.SUPPRESS on subcommands)."""
    parser.add_argument("--solisid",  default=d(env.get("UU_SOLISID")))
    parser.add_argument("--password", default=d(env.get("UU_PASSWORD", "")),
                        help="Visible in `ps` and shell history; prefer `uu save-creds`")
    parser.add_argument("--totp",     default=d(env.get("UU_TOTP_SECRET", "")), type=totp_secret,
                        help="TOTP secret key (base32), not a 6-digit code; prefer `uu save-creds`")
    parser.add_argument("--auth", choices=["auto", *ROUTES, "browser"],
                        default=d(env.get("UU_AUTH", "auto")),
                        help="How to log in: totp-secret generates the one-time code, "
                             "prompt asks for the code from your authenticator app, "
                             "browser opens a window where you log in yourself; "
                             "auto (default) uses the secret when one is set, else prompt")


def totp_secret(value: str) -> str:
    """--totp's type: a 6-digit code there is a mistake, as it lasts only 30 seconds."""
    if value.strip().isdigit():
        raise argparse.ArgumentTypeError(
            "expects your TOTP secret key (e.g. JBSWY3DPEHPK3PXP), not a one-time code; "
            "see README 'Getting your TOTP secret'")
    return value


def interactive() -> bool:
    """Whether someone is at the terminal to type a code."""
    return sys.stdin.isatty()


def has_screen() -> bool:
    """Whether a browser window can open here (on Linux: DISPLAY or WAYLAND_DISPLAY, as WSLg sets)."""
    return not sys.platform.startswith("linux") or bool(os.environ.get("DISPLAY")
                                                         or os.environ.get("WAYLAND_DISPLAY"))


def ready_to_login(args) -> bool:
    """Fill in what a login needs and settle args.auth; True when a login can go ahead.

    Missing details come from the encrypted credentials file first. Without a TOTP secret the
    prompt route asks you for the code, and for a missing Solis-id or password too; that needs a
    terminal, so unattended runs still need the secret. The browser route needs nothing but a
    screen to open its window on.
    """
    if args.auth == "browser":
        if not has_screen():
            print("ERROR: --auth browser needs a screen to open its window on "
                  "(DISPLAY or WAYLAND_DISPLAY is not set).", file=sys.stderr)
            return False
        return True
    have_creds(args)
    if args.auth == "auto":
        args.auth = "totp-secret" if args.totp or not interactive() else "prompt"
    if args.auth == "totp-secret":
        return bool(args.password and args.totp)
    if not interactive():
        print("ERROR: --auth prompt needs a terminal to type the code in.", file=sys.stderr)
        return False
    args.solisid = args.solisid or input("Solis-id: ").strip()
    args.password = args.password or getpass.getpass("Password: ")
    return bool(args.solisid and args.password)


def otp_route(args) -> OtpRoute:
    if args.auth == "prompt" or (args.auth == "auto" and not args.totp):
        return PromptCode()
    return TotpSecret(args.totp)
