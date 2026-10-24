"""`uu osiris <command>`."""

from __future__ import annotations

import argparse
import sys

from uu_cli.services.osiris import api
from uu_cli.services.osiris.commands import cmd_filters, cmd_search, cmd_show

COMMANDS = {
    "search": cmd_search,
    "show": cmd_show,
    "filters": cmd_filters,
}


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    p = argparse.ArgumentParser(
        prog=prog, description="Osiris Student's course catalogue (no login needed)",
        epilog=f"The same catalogue as {api.CATALOGUE}.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--year", help="Academic year, 2026 or 2026-2027 (default: the "
                                       "catalogue's current one)")
    common.add_argument("--lang", choices=["en", "nl"], default="en",
                        help="Language of names, descriptions and filter values (default: en)")
    common.add_argument("--json", action="store_true", help="Print JSON")

    sub = p.add_subparsers(dest="cmd", required=True)
    se = sub.add_parser("search", parents=[common], help="Search courses by code, name or filters",
                        description="Search courses. Filter values match by code (level M), "
                                    "word (faculty science) or part, in any case; give a filter "
                                    "twice for either value. `uu osiris filters` lists them all.")
    se.add_argument("query", nargs="?", help="The start of a word in the course code or name")
    for name, (_, _, title) in api.FILTERS.items():
        se.add_argument(f"--{name}", action="append", metavar="VALUE", help=title)
    se.add_argument("--teacher", help="Part of a lecturer's name")
    se.add_argument("--open", action="store_true", help="Only courses open for enrolment now")
    se.add_argument("--limit", type=int, default=25, help="How many to show (default 25)")
    sh = sub.add_parser("show", parents=[common], help="A course's page: description, "
                                                      "enrolment, tests, lecturers")
    sh.add_argument("code", help="The course code, e.g. INFOB1PICA")
    sub.add_parser("filters", parents=[common], help="The values --faculty, --level, --period, "
                                                    "--timeslot, --language and --type take")

    args = p.parse_args(argv)
    if args.cmd == "search" and not 1 <= args.limit <= 1000:
        p.error("--limit must be 1 to 1000")
    try:
        sys.exit(COMMANDS[args.cmd](args))
    except api.OsirisError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
