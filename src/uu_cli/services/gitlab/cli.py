"""`uu gitlab <command>`."""

from __future__ import annotations

import sys

from uu_cli.services import common
from uu_cli.services.gitlab.api import HOST, GitLabError, NotLoggedIn
from uu_cli.services.gitlab.commands import cmd_add_key, cmd_create, cmd_login, cmd_projects

COMMANDS = {
    "login": cmd_login,
    "projects": cmd_projects,
    "create": cmd_create,
    "add-key": cmd_add_key,
}

login = cmd_login
OWN_FILES = HOST


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    p, cmd = common.command_line(
        prog, f"GitLab ({HOST}): the Faculty of Science's GitLab",
        epilog="GitLab asks no one-time code: every --auth but browser fills in its Solis-ID "
               "sign-in with your Solis-id and password.")

    cmd("login", help="Log in with your Solis-id now (same as `uu login gitlab`)")
    cmd("projects", help="List the projects you own or are a member of")
    new = cmd("create", help="Create an empty project, for an existing repo's first push")
    new.add_argument("name", help="The project's name; its address follows from it")
    new.add_argument("--description")
    new.add_argument("--visibility", choices=["private", "internal", "public"], default="private",
                     help="private (default): only you and members you add; internal: everyone "
                          f"who can log in to {HOST}; public: anyone")
    key = cmd("add-key", help="Add your public SSH key to your account, so git can push over SSH")
    key.add_argument("file", nargs="?", default="~/.ssh/id_ed25519.pub",
                     help="Your public key (default: ~/.ssh/id_ed25519.pub)")
    key.add_argument("--title", help="The key's name in GitLab (default: this computer's name)")

    args = p.parse_args(argv)
    common.run(_run, args, own_files=OWN_FILES)


def _run(ctx, args) -> int:
    try:
        return COMMANDS[args.cmd](ctx, args)
    except GitLabError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    except NotLoggedIn:
        print("ERROR: GitLab ended the session; run `uu login gitlab` again.", file=sys.stderr)
        return 1
