"""`uu filesender <command>`."""

from __future__ import annotations

import argparse
import sys

from uu_cli import auth, paths
from uu_cli.services.filesender.commands import cmd_login, cmd_send, cmd_transfers


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    paths.ensure_state_dirs()

    p = argparse.ArgumentParser(
        prog=prog, description="SURFfilesender: send large files to named people, for a short while")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_login(parser):
        parser.add_argument("--session-dir", default=str(paths.SESSION_DIR),
                            help="Browser profile, shared with the other services (one UU login)")
        auth.add_login_args(parser, lambda v: v)
        return parser

    with_login(sub.add_parser("login", help="Log in through SURFconext and UU (and keep the session)"))

    se = with_login(sub.add_parser("send", help="Send files to one or more people by email"))
    se.add_argument("files", nargs="+", help="The files to send (a folder: zip it first)")
    se.add_argument("--to", action="append", required=True,
                    help="Recipient's email address; repeat it, or separate addresses with commas")
    se.add_argument("--subject")
    se.add_argument("--message")
    se.add_argument("--days", type=int,
                    help="Days the files stay available (SURF: 14 by default, at most 21)")
    se.add_argument("--encrypt", action="store_true",
                    help="Encrypt with a password you type (or UU_FILESENDER_PASSWORD); "
                         "files up to 2 GB")
    se.add_argument("--accept-terms", action="store_true",
                    help="You accept SURFfilesender's code of conduct, as the upload page asks")

    tr = with_login(sub.add_parser("transfers", help="Your transfers that haven't expired"))
    tr.add_argument("--json", action="store_true", help="Print them as JSON")

    args = p.parse_args(argv)
    sys.exit({"login": cmd_login, "send": cmd_send, "transfers": cmd_transfers}[args.cmd](args))
