"""`uu osiris <command>`."""

from __future__ import annotations

import argparse
import sys

from uu_cli.services import common
from uu_cli.services.osiris import api, student
from uu_cli.services.osiris.commands import cmd_filters, cmd_search, cmd_show

# The public course catalogue: no login, no browser.
COMMANDS = {
    "search": cmd_search,
    "show": cmd_show,
    "filters": cmd_filters,
}

# Your own Osiris Student, logged in with your Solis-id, in the browser the other services share.
login = student.cmd_login
OWN_FILES = None


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    p, cmd = common.command_line(
        prog, "Osiris Student's course catalogue (no login needed)",
        epilog=f"The same catalogue as {api.CATALOGUE}.")
    catalogue = argparse.ArgumentParser(add_help=False)
    catalogue.add_argument("--year", help="Academic year, 2026 or 2026-2027 (default: the "
                                          "catalogue's current one)")
    catalogue.add_argument("--lang", choices=["en", "nl"], default="en",
                           help="Language of names, descriptions and filter values (default: en)")
    catalogue.add_argument("--json", action="store_true", help="Print JSON")

    se = cmd("search", login=False, parents=[catalogue],
             help="Search courses by code, name or filters",
             description="Search courses. Filter values match by code (level M), word (faculty "
                         "science) or part, in any case; give a filter twice for either value. "
                         "`uu osiris filters` lists them all.")
    se.add_argument("query", nargs="?", help="The start of a word in the course code or name")
    for name, (_, _, title) in api.FILTERS.items():
        se.add_argument(f"--{name}", action="append", metavar="VALUE", help=title)
    se.add_argument("--teacher", help="Part of a lecturer's name")
    se.add_argument("--open", action="store_true", help="Only courses open for enrolment now")
    se.add_argument("--limit", type=int, default=25, help="How many to show (default 25)")
    sh = cmd("show", login=False, parents=[catalogue],
             help="A course's page: description, enrolment, tests, lecturers")
    sh.add_argument("code", help="The course code, e.g. INFOB1PICA")
    cmd("filters", login=False, parents=[catalogue],
        help="The values --faculty, --level, --period, --timeslot, --language and --type take")
    cmd("login", help="Log in to Osiris Student with your Solis-id (same as `uu login osiris`)")

    args = p.parse_args(argv)
    if args.cmd == "search" and not 1 <= args.limit <= 1000:
        p.error("--limit must be 1 to 1000")
    if args.cmd == "login":
        common.run(login, args)
    try:
        sys.exit(COMMANDS[args.cmd](args))
    except api.OsirisError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
