"""`uu bookings <command>`."""

from __future__ import annotations

import sys

from uu_cli.services import common
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

login = cmd_login
OWN_FILES = None


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    p, cmd = common.command_line(
        prog, "bookings.uu.nl: the University Library's study spaces and rooms")

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

    cmd("login", help="Log in through UU now (same as `uu login bookings`)")
    cmd("places", login=False, help="The booking pages: study spaces, group spaces, meeting rooms")
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

    li = cmd("list", login=False, help="Your upcoming bookings made with `book` (the site lists none)")
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
    common.run(COMMANDS[args.cmd], args)
