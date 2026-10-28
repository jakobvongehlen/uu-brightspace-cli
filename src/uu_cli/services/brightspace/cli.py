"""`uu brightspace <command>` (and `python3 brightspace-cli.py <command>`)."""

from __future__ import annotations

import argparse
import os
import sys

from playwright.sync_api import sync_playwright

from uu_cli import auth, browser, paths
from uu_cli.auth.credentials import cmd_save_creds
from uu_cli.login import log_in
from uu_cli.services.brightspace.api import (
    BASE_URL, EXPIRED_MSG, extract_token, load_token, refresh_token, save_token, token_expired,
)
from uu_cli.services.brightspace.commands import (
    cmd_assignments, cmd_courses, cmd_deadlines, cmd_grades, cmd_login, cmd_news,
    cmd_schedule, cmd_toc,
)
from uu_cli.services.brightspace.discussions import cmd_discussions
from uu_cli.services.brightspace.sync import cmd_sync, cmd_sync_all


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    paths.ensure_state_dirs()

    def add_global_args(parser, suppress=False):
        # Subparsers use SUPPRESS so they don't overwrite values given before the subcommand
        d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
        parser.add_argument("--session-dir", default=d(str(paths.SESSION_DIR)))
        auth.add_login_args(parser, d)
        parser.add_argument("--token",    default=d(None), help="Bearer token (skips login if provided)")

    p = argparse.ArgumentParser(prog=prog, description="Brightspace CLI (pure Playwright)")
    add_global_args(p)
    common = argparse.ArgumentParser(add_help=False)
    add_global_args(common, suppress=True)

    sub = p.add_subparsers(dest="cmd", required=True)
    def cmd(*a, **kw):
        return sub.add_parser(*a, parents=[common], **kw)

    cmd("save-creds",  help="Store Solis-id, password and (optionally) TOTP secret encrypted")
    cmd("login",       help="Authenticate and cache token")
    cmd("courses",     help="List enrolled courses")
    toc = cmd("toc",          help="Show course TOC")
    toc.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    ass = cmd("assignments",  help="List assignments (dropbox + LTI links)")
    ass.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    grd = cmd("grades",       help="Show grades")
    grd.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    nws = cmd("news",         help="Show announcements")
    nws.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    dsc = cmd("discussions",  help="Show the discussion forums, thread by thread")
    dsc.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    sch = cmd("schedule",     help="Download Google Sheets from course content")
    sch.add_argument("-o", "--output",   default=None)
    sch.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    ddl = cmd("deadlines",    help="Show all upcoming deadlines")
    ddl.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    syn = cmd("sync",         help="Sync all course content")
    syn.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    syn.add_argument("-o", "--output",   default=None)

    syn_all = cmd("sync-all", help="Sync every enrolled course")
    syn_all.add_argument("-o", "--output", default=None)
    syn_all.add_argument("--dry-run", action="store_true",
                         help="List selected courses without downloading content")

    args = p.parse_args(argv)

    if args.totp and args.totp.strip().isdigit():
        p.error("--totp expects your TOTP secret key (e.g. JBSWY3DPEHPK3PXP), "
                "not a one-time code; see README 'Getting your TOTP secret'")
    if args.cmd == "save-creds":
        sys.exit(cmd_save_creds(args))
    if args.cmd not in ("login", "courses", "sync-all") and not args.org_unit:
        p.error("--org-unit or BRIGHTSPACE_ORG_UNIT required for content commands")
    if (args.cmd == "login" and not args.token and not args.solisid
            and args.auth != "browser" and not paths.CREDS_FILE.exists()
            and not (args.auth != "totp-secret" and auth.interactive())):
        p.error("--solisid or BRIGHTSPACE_SOLISID (or save-creds) required for login")

    with sync_playwright() as pw:
        ctx = browser.launch(pw, args.session_dir)
        try:
            _run(ctx, args)
        finally:
            browser.save_session(ctx, args.session_dir)


def _run(ctx, args) -> None:
    if args.token:
        save_token(args.token)
        print("Token provided and cached.", file=sys.stderr)

    elif args.cmd == "login":
        if not auth.ready_to_login(args):
            print(f"ERROR: Cannot log in: {auth.CANNOT_LOGIN}.", file=sys.stderr)
            sys.exit(1)
        log_in(ctx, BASE_URL, args)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE_URL}/d2l/home", wait_until="load", timeout=15000)
        page.wait_for_timeout(2000)
        token = extract_token(ctx)
        if token:
            save_token(token)
            print("Token cached.", file=sys.stderr)
        sys.exit(0)

    else:
        cached = load_token()
        if cached and cached.get("token") and token_expired(cached["token"]):
            fresh = refresh_token(ctx)  # a saved session can mint a new token without a login
            if fresh:
                cached = {"token": fresh}
            elif args.cmd != "sync":  # sync logs in by itself
                sys.exit(EXPIRED_MSG)
        if cached and cached.get("token"):
            print(f"[MAIN] Using cached token from {paths.TOKEN_FILE}",
                  file=sys.stderr)
            args.token = cached["token"]
        else:
            if not auth.ready_to_login(args):
                print(f"ERROR: No cached token, and {auth.CANNOT_LOGIN}.", file=sys.stderr)
                sys.exit(1)
            print("[MAIN] No token — logging in...", file=sys.stderr)
            log_in(ctx, BASE_URL, args)
            page = ctx.pages[0]
            # Do not re-navigate — log_in already lands on /d2l/home
            page.wait_for_timeout(2000)
            token = extract_token(ctx)
            if token:
                save_token(token)
            args.token = token

    cmds = {
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
    sys.exit(cmds[args.cmd](ctx, args))
