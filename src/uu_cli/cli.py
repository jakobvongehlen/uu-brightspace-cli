"""The `uu` command: `uu <service> <command> [options]`, and `uu login` and `uu save-creds` for all of them."""

from __future__ import annotations

import argparse
import importlib
import sys

from uu_cli import __version__, paths

# Service name -> (module whose main(argv, prog) runs that service's commands, what it is).
SERVICES = {
    "bookings": ("uu_cli.services.bookings.cli", "the library's study spaces (bookings.uu.nl)"),
    "brightspace": ("uu_cli.services.brightspace.cli", "your courses on Brightspace"),
    "edunl": ("uu_cli.services.edunl.cli", "SURF's URL shortener (edu.nl)"),
    "filesender": ("uu_cli.services.filesender.cli", "send large files (SURFfilesender)"),
    "gitlab": ("uu_cli.services.gitlab.cli", "the Faculty of Science's GitLab"),
    "konjoin": ("uu_cli.services.konjoin.cli", "research and thesis projects (Konjoin)"),
    "library": ("uu_cli.services.library.cli", "journal articles through UU's library"),
    "osiris": ("uu_cli.services.osiris.cli", "the course catalogue (Osiris Student)"),
    "remindo": ("uu_cli.services.remindo.cli", "your exams and results (Remindo)"),
}

# Old service names that keep working.
ALIASES = {"papers": "library"}

# The services `uu login` takes. The library has no login of its own: you log in in its
# window, when UU asks.
LOGINS = [name for name in SERVICES if name != "library"]


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ALIASES:
        argv = [ALIASES[argv[0]], *argv[1:]]
    if argv and argv[0] in SERVICES:
        paths.ensure_state_dirs()
        service = importlib.import_module(SERVICES[argv[0]][0])
        service.main(argv[1:], prog=f"uu {argv[0]}")
        return
    if argv and argv[0] in ACCOUNT:
        paths.ensure_state_dirs()
        sys.exit(ACCOUNT[argv[0]][0](argv[1:]))

    p = argparse.ArgumentParser(
        prog="uu",
        description="Headless access to Utrecht University platforms.",
        epilog="Run 'uu <service> --help' for the commands of a service.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(title="commands", metavar="<command>", required=True)
    for name, (_, what) in ACCOUNT.items():
        sub.add_parser(name, help=what)
    for name, (_, what) in SERVICES.items():
        sub.add_parser(name, help=what)
    p.parse_args(argv)


def login(argv: list[str]) -> int:
    """`uu login <service>...`: log in to each now, in one browser, with one set of login details."""
    from uu_cli import browser
    from uu_cli.services import common

    p = argparse.ArgumentParser(
        prog="uu login", description="Log in to services now and keep their sessions. Every "
        "command also logs in by itself when it needs to, so this is for getting it done ahead "
        "of time. While UU's session lasts, the next service logs in without a code; "
        "bookings asks one anyway, and GitLab has its own sign-in.")
    p.add_argument("services", nargs="+", choices=LOGINS, metavar="service",
                   help=", ".join(LOGINS))
    common.add_login_options(p)
    args = p.parse_args(argv)

    failed = []
    with common.browser_for(args.session_dir) as ctx:
        for name in dict.fromkeys(args.services):
            service = importlib.import_module(SERVICES[name][0])
            if service.OWN_FILES:
                browser.own_files_only(ctx, service.OWN_FILES)
            try:
                status = service.login(ctx, args)
            except Exception as err:  # one service's page broke: the others may still work
                print(f"ERROR: {name}: {err}", file=sys.stderr)
                status = 1
            if status:
                failed.append(name)
    if failed:
        print(f"Not logged in to {', '.join(failed)}.", file=sys.stderr)
    return 1 if failed else 0


def save_creds(argv: list[str]) -> int:
    """`uu save-creds`: the login details every service uses, encrypted with a passphrase."""
    from uu_cli import auth
    from uu_cli.auth.credentials import cmd_save_creds

    p = argparse.ArgumentParser(
        prog="uu save-creds", description=f"Store your Solis-id, password and (optionally) TOTP "
        f"secret encrypted, in {paths.CREDS_FILE}. Every service's login uses them; a login "
        "asks for the passphrase (or takes UU_PASSPHRASE).")
    auth.add_login_args(p)
    return cmd_save_creds(p.parse_args(argv))


# Commands for every service at once -> (function(argv), what it does).
ACCOUNT = {
    "login": (login, "log in to one or more services now"),
    "save-creds": (save_creds, "store your Solis-id, password and TOTP secret encrypted"),
}
