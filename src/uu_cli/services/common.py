"""What every `uu <service>` shares: the login options on its command line, and the browser a
command runs in."""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Iterator, NoReturn

from uu_cli import auth, paths

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

# A command that runs in the browser: (browser, options) -> exit status.
Command = Callable[["BrowserContext", argparse.Namespace], int]


def add_login_options(parser: argparse.ArgumentParser, d=lambda v: v) -> None:
    """--session-dir and the login options (uu_cli.auth). d wraps defaults (argparse.SUPPRESS on commands)."""
    parser.add_argument("--session-dir", default=d(str(paths.SESSION_DIR)),
                        help="Browser profile, shared by every service (one UU login)")
    auth.add_login_args(parser, d)


def command_line(prog: str | None, description: str, options=add_login_options, **kw):
    """A service's parser, and cmd(name, help=...) that adds one of its commands.

    A command takes options (the login options, unless login=False) after its name as well as
    before it: `uu konjoin --auth prompt login` and `uu konjoin login --auth prompt`.
    """
    p = argparse.ArgumentParser(prog=prog, description=description, **kw)
    options(p)
    # SUPPRESS: an option given before the command isn't overwritten by the command's default.
    after = argparse.ArgumentParser(add_help=False)
    options(after, lambda v: argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, login: bool = True, parents=(), **kw) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[*([after] if login else []), *parents], **kw)

    return p, cmd


@contextmanager
def browser_for(session_dir: str, own_files: str | None = None) -> Iterator[BrowserContext]:
    """The shared browser profile with the last run's session, open for a command; saves the session after.

    own_files: a host whose pages load only its own files (browser.own_files_only).
    """
    from playwright.sync_api import sync_playwright

    from uu_cli import browser

    paths.ensure_state_dirs()
    with sync_playwright() as pw:
        ctx = browser.launch(pw, session_dir)
        if own_files:
            browser.own_files_only(ctx, own_files)
        try:
            yield ctx
        finally:
            browser.save_session(ctx, session_dir)


def run(command: Command, args: argparse.Namespace, own_files: str | None = None) -> NoReturn:
    """Run command in the browser and exit with its status."""
    with browser_for(args.session_dir, own_files) as ctx:
        sys.exit(command(ctx, args))
