"""`uu edunl <command>`."""

from __future__ import annotations

from uu_cli.services import common
from uu_cli.services.edunl.api import HOST
from uu_cli.services.edunl.commands import cmd_links, cmd_login, cmd_shorten

COMMANDS = {
    "login": cmd_login,
    "shorten": cmd_shorten,
    "links": cmd_links,
}

login = cmd_login
OWN_FILES = HOST


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    p, cmd = common.command_line(
        prog, "edu.nl: SURF's URL shortener for education and research")

    cmd("login", help="Log in through SURFconext and UU now (same as `uu login edunl`)")
    sh = cmd("shorten", help="Make a short edu.nl link; prints the link")
    sh.add_argument("url", help="The web address to shorten (http:// or https://)")
    sh.add_argument("--label", help="A label for the link, which only you see in your overview")
    li = cmd("links", help="Your links, newest first")
    li.add_argument("-s", "--search", help="Search term, as in the overview's search box")
    li.add_argument("--page", type=int, default=1)
    li.add_argument("--json", action="store_true", help="Print the links as JSON")

    args = p.parse_args(argv)
    common.run(COMMANDS[args.cmd], args, own_files=OWN_FILES)
