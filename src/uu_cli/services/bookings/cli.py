"""`uu bookings <command>`."""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

from uu_cli import auth, browser, paths
from uu_cli.services.bookings import api
from uu_cli.services.bookings.commands import (cmd_book, cmd_cancel, cmd_list, cmd_login,
                                               cmd_options, cmd_places, cmd_search, cmd_show)

COMMANDS = {
    "login": cmd_login,
    "options": cmd_options,
    "search": cmd_search,
    "book": cmd_book,
    "show": cmd_show,
    "cancel": cmd_cancel,
}


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    paths.ensure_state_dirs()

    def add_global_args(parser, suppress=False):
        # Subparsers use SUPPRESS so they don't overwrite values given before the subcommand
        d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
        parser.add_argument("--session-dir", default=d(str(paths.SESSION_DIR)),
                            help="Browser profile, shared with the other services (one UU login)")
        auth.add_login_args(parser, d)

    p = argparse.ArgumentParser(
        prog=prog, description="bookings.uu.nl: the University Library's study spaces and rooms")
    add_global_args(p)
    common = argparse.ArgumentParser(add_help=False)
    add_global_args(common, suppress=True)

    sub = p.add_subparsers(dest="cmd", required=True)
    def cmd(*a, **kw):
        return sub.add_parser(*a, parents=[common], **kw)

    def when(parser):
        parser.add_argument("place", choices=api.PLACES, metavar="place",
                            help="The booking page (see `uu bookings places`)")
        parser.add_argument("--date", help="YYYY-MM-DD (default: today; at most 2 days ahead)")
        parser.add_argument("--from", dest="start", help="Start time, HH:MM")
        parser.add_argument("--until", dest="end", help="End time, HH:MM")
        parser.add_argument("--category", help="By number or name (see `uu bookings options`)")
        parser.add_argument("--type", help="Type of space, by number or name")
        parser.add_argument("--zone", help="Floor or zone, by number or name")
        for name in api.FILTERS:
            parser.add_argument(f"--{name}", action="store_true", help=f"Only spaces with {name}"
                                if name != "accessible" else "Only accessible spaces")

    cmd("login", help="Log in through SURFconext and UU (and keep the session)")
    sub.add_parser("places", help="The booking pages: study spaces, group spaces, meeting rooms")
    op = cmd("options", help="A page's categories, types of space and zones")
    op.add_argument("place", choices=api.PLACES, metavar="place")
    op.add_argument("--json", action="store_true")
    se = cmd("search", help="Free spaces for a date and time")
    when(se)
    se.add_argument("--json", action="store_true", help="Print the results as JSON")
    bo = cmd("book", help="Book a space (by its id or name, from `search`)")
    when(bo)
    bo.add_argument("space", help="The space's id or name, as `search` lists it")
    bo.add_argument("--accept-terms", action="store_true",
                    help="You agree to the page's Terms & Conditions (the library's house rules)")

    li = sub.add_parser("list", help="Your upcoming bookings made with `book` (the site lists none)")
    li.add_argument("--all", action="store_true", help="Also past and cancelled ones")
    li.add_argument("--json", action="store_true")
    sh = cmd("show", help="The bookings on a confirmation, from the email's cancel link")
    sh.add_argument("link", help="The cancel link from the confirmation email, or its code")
    ca = cmd("cancel", help="Cancel the bookings on a confirmation (the email's cancel link)")
    ca.add_argument("link", help="The cancel link from the confirmation email, or its code")
    ca.add_argument("--booking", help="Only this booking id (from `show`); default: all on it")

    args = p.parse_args(argv)
    if args.cmd in ("places", "list"):
        sys.exit({"places": cmd_places, "list": cmd_list}[args.cmd](None, args))

    with sync_playwright() as pw:
        ctx = browser.launch(pw, args.session_dir)
        try:
            sys.exit(COMMANDS[args.cmd](ctx, args))
        finally:
            browser.save_session(ctx, args.session_dir)
