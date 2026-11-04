"""`uu remindo <command>`."""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

from uu_cli import auth, browser, paths
from uu_cli.services.remindo.api import RESULT_TABS
from uu_cli.services.remindo.commands import cmd_agenda, cmd_login, cmd_export, cmd_page, cmd_result, cmd_results

COMMANDS = {
    "login": cmd_login,
    "agenda": cmd_agenda,
    "results": cmd_results,
    "result": cmd_result,
    "export": cmd_export,
    "page": cmd_page,
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
        prog=prog, description="Remindo: UU's digital exam environment (remindo.uu.nl)")
    add_global_args(p)
    common = argparse.ArgumentParser(add_help=False)
    add_global_args(common, suppress=True)

    sub = p.add_subparsers(dest="cmd", required=True)
    def cmd(*a, **kw):
        return sub.add_parser(*a, parents=[common], **kw)

    cmd("login", help="Log in with your Solis-id (and keep the session)")
    ag = cmd("agenda", help="Upcoming exams and practices")
    ag.add_argument("--json", action="store_true")
    rs = cmd("results", help="Your attempts: exams, graded practices and practices, with their ids")
    rs.add_argument("--type", choices=["all", *RESULT_TABS], default="all")
    rs.add_argument("--new", action="store_true",
                    help="Only what is new or changed since the last --new (quiet when nothing is; for cron)")
    rs.add_argument("--json", action="store_true")
    re_ = cmd("result", help="One result, by its id from `results`")
    re_.add_argument("id", help="The result's id, as `results` lists it")
    re_.add_argument("--questions", action="store_true",
                     help="Every question, with your answer and the correct one")
    re_.add_argument("--json", action="store_true")
    ex = cmd("export", help="Save results as text and JSON, with their images, a folder per course")
    ex.add_argument("ids", nargs="*", help="Result ids, as `results` lists them")
    ex.add_argument("--all", action="store_true", help="Every result Remindo lets you view")
    ex.add_argument("--out", default=".", help="Folder to save into (default: the current one)")
    ex.add_argument("--force", action="store_true", help="Save again what is already saved")
    ex.add_argument("--no-images", action="store_true", help="Leave the images as links")
    pg = cmd("page", help="A Remindo page as text, with its links")
    pg.add_argument("path", nargs="?", default="/", help="Path or link on remindo.uu.nl (default: /)")
    pg.add_argument("--json", action="store_true", help="Print title, text and links as JSON")

    args = p.parse_args(argv)

    with sync_playwright() as pw:
        ctx = browser.launch(pw, args.session_dir)
        try:
            sys.exit(COMMANDS[args.cmd](ctx, args))
        finally:
            browser.save_session(ctx, args.session_dir)
