"""`uu osiris <command>`."""

from __future__ import annotations

import argparse
import sys

from uu_cli import auth, paths
from uu_cli.services.osiris import api, student
from uu_cli.services.osiris.commands import cmd_filters, cmd_search, cmd_show

COMMANDS = {
    "search": cmd_search,
    "show": cmd_show,
    "filters": cmd_filters,
}

# Commands on your own Osiris Student: in the browser, logged in.
BROWSER_COMMANDS = {
    "login": lambda ctx, args: 0 if student.ensure_login(ctx, args) else 1,
}


def _in_browser(fn, args) -> int:
    from playwright.sync_api import sync_playwright

    from uu_cli import browser
    paths.ensure_state_dirs()
    with sync_playwright() as pw:
        ctx = browser.launch(pw, args.session_dir)
        try:
            return fn(ctx, args)
        finally:
            browser.save_session(ctx, args.session_dir)


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

    # Your own Osiris Student, logged in with your Solis-id, in the browser the other services share.
    logged_in = argparse.ArgumentParser(add_help=False)
    logged_in.add_argument("--session-dir", default=str(paths.SESSION_DIR),
                           help="Browser profile, shared with the other services (one UU login)")
    auth.add_login_args(logged_in, lambda v: v)
    sub.add_parser("login", parents=[logged_in], help="Log in to Osiris Student with your Solis-id")

    args = p.parse_args(argv)
    if args.cmd == "search" and not 1 <= args.limit <= 1000:
        p.error("--limit must be 1 to 1000")
    if args.cmd in BROWSER_COMMANDS:
        sys.exit(_in_browser(BROWSER_COMMANDS[args.cmd], args))
    try:
        sys.exit(COMMANDS[args.cmd](args))
    except api.OsirisError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
