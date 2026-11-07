"""`uu brightspace <command>` (and `python3 brightspace-cli.py <command>`)."""

from __future__ import annotations

import sys

from uu_cli import env, paths
from uu_cli.auth.credentials import cmd_save_creds
from uu_cli.services import common
from uu_cli.services.brightspace.api import save_token
from uu_cli.services.brightspace.commands import (
    cmd_assignments, cmd_courses, cmd_deadlines, cmd_grades, cmd_login, cmd_schedule, cmd_toc,
)
from uu_cli.services.brightspace.discussions import cmd_discussions
from uu_cli.services.brightspace.news import cmd_news, since_date
from uu_cli.services.brightspace.sync import cmd_sync, cmd_sync_all

COMMANDS = {
    "login":       cmd_login,
    "courses":     cmd_courses,
    "toc":         cmd_toc,
    "assignments": cmd_assignments,
    "grades":      cmd_grades,
    "news":        cmd_news,
    "discussions": cmd_discussions,
    "schedule":    cmd_schedule,
    "deadlines":   cmd_deadlines,
    "sync":        cmd_sync,
    "sync-all":    cmd_sync_all,
}

login = cmd_login
OWN_FILES = None


def _options(parser, d=lambda v: v) -> None:
    common.add_login_options(parser, d)
    parser.add_argument("--token", default=d(None), help="Bearer token (skips login if provided)")


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    paths.ensure_state_dirs()  # brightspace-cli.py comes here without `uu`
    p, cmd = common.command_line(prog, "Brightspace (uu.brightspace.com): your courses",
                                 options=_options)

    def course(parser):
        parser.add_argument("--org-unit", default=env.get("UU_BRIGHTSPACE_ORG_UNIT"))
        return parser

    cmd("save-creds",  help="Store Solis-id, password and (optionally) TOTP secret encrypted "
                            "(same as `uu save-creds`)")
    cmd("login",       help="Log in and cache a token (same as `uu login brightspace`)")
    cmd("courses",     help="List enrolled courses")
    course(cmd("toc",          help="Show course TOC"))
    course(cmd("assignments",  help="List assignments (dropbox + LTI links)"))
    course(cmd("grades",       help="Show grades"))
    news = course(cmd("news",  help="Show announcements: one course's, or every course's"))
    news.add_argument("--all-courses", action="store_true",
                      help="Every enrolled course's announcements, in one run (no --org-unit)")
    news.add_argument("--since", type=since_date, default=None, metavar="YYYY-MM-DD",
                      help="Only announcements from this date on")
    news.add_argument("--json", action="store_true",
                      help="Print JSON: a list of announcements, or with --all-courses "
                           "{since, courses, items, failed}")
    course(cmd("discussions",  help="Show the discussion forums, thread by thread"))
    sch = course(cmd("schedule",     help="Download Google Sheets from course content"))
    sch.add_argument("-o", "--output",   default=None)
    course(cmd("deadlines",    help="Show all upcoming deadlines"))
    syn = course(cmd("sync",         help="Sync all course content"))
    syn.add_argument("-o", "--output",   default=None)

    syn_all = cmd("sync-all", help="Sync every enrolled course")
    syn_all.add_argument("-o", "--output", default=None)
    syn_all.add_argument("--dry-run", action="store_true",
                         help="List selected courses without downloading content")

    args = p.parse_args(argv)

    if args.cmd == "save-creds":
        sys.exit(cmd_save_creds(args))
    every_course = args.cmd == "sync-all" or getattr(args, "all_courses", False)
    if args.cmd not in ("login", "courses") and not every_course and not args.org_unit:
        p.error("--org-unit or UU_BRIGHTSPACE_ORG_UNIT required for content commands")
    common.run(_run, args)


def _run(ctx, args) -> int:
    if args.token:
        save_token(args.token)
        print("Token provided and cached.", file=sys.stderr)
    return COMMANDS[args.cmd](ctx, args)
