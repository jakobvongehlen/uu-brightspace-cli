"""`uu konjoin <command>`."""

from __future__ import annotations

from urllib.parse import urlparse

from uu_cli.services import common
from uu_cli.services.konjoin.api import BASE_URL
from uu_cli.services.konjoin.commands import cmd_export, cmd_login, cmd_project, cmd_projects

COMMANDS = {
    "login": cmd_login,
    "projects": cmd_projects,
    "project": cmd_project,
    "export": cmd_export,
}

login = cmd_login
OWN_FILES = urlparse(BASE_URL).hostname


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    p, cmd = common.command_line(
        prog, "Konjoin (uu.konjoin.nl): UU's research and thesis projects")

    cmd("login", help="Log in through UU now (same as `uu login konjoin`)")
    prj = cmd("projects", help="List projects; public ones need no login")
    prj.add_argument("-s", "--search", help="Search term, as in the site's search box")
    prj.add_argument("--page", type=int, default=1)
    prj.add_argument("--all", action="store_true", help="Every page instead of one")
    prj.add_argument("--json", action="store_true", help="Print the raw project data")
    one = cmd("project", help="Show one project (needs a login)")
    one.add_argument("slug", help="The project's slug, as listed by `projects` (e.g. 3208-some-title)")
    one.add_argument("--json", action="store_true", help="Print the raw project data")
    exp = cmd("export", help="Save every project to a JSON file")
    exp.add_argument("-o", "--output", required=True, help="JSON file to write")
    exp.add_argument("-s", "--search", help="Only projects matching this search term")
    for sp in (prj, one, exp):
        sp.add_argument("--keep-contact-details", action="store_true",
                        help="Keep people's email addresses and organisation ids in JSON output "
                             "(left out by default)")

    args = p.parse_args(argv)
    common.run(COMMANDS[args.cmd], args, own_files=OWN_FILES)
