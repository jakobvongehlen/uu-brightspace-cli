"""`uu filesender <command>`."""

from __future__ import annotations

import sys
from urllib.parse import urlparse

from uu_cli.services import common
from uu_cli.services.filesender.api import BASE_URL
from uu_cli.services.filesender.commands import cmd_login, cmd_send, cmd_transfers

login = cmd_login
OWN_FILES = urlparse(BASE_URL).hostname


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    p, cmd = common.command_line(
        prog, "SURFfilesender: send large files to named people, for a short while")

    cmd("login", help="Log in through SURFconext and UU now (same as `uu login filesender`)")
    se = cmd("send", help="Send files to one or more people by email")
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
    tr = cmd("transfers", help="Your transfers that haven't expired")
    tr.add_argument("--json", action="store_true", help="Print them as JSON")

    args = p.parse_args(argv)
    if args.cmd == "login":
        common.run(cmd_login, args, own_files=OWN_FILES)
    # send and transfers check what they're given before they open the browser (connected)
    sys.exit({"send": cmd_send, "transfers": cmd_transfers}[args.cmd](args))
