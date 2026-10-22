"""The `uu` command: `uu <service> <command> [options]`."""

from __future__ import annotations

import argparse
import importlib
import sys

from uu_cli import __version__

# Service name -> module whose main(argv, prog) runs that service's commands.
SERVICES = {
    "brightspace": "uu_cli.services.brightspace.cli",
    "konjoin": "uu_cli.services.konjoin.cli",
    "library": "uu_cli.services.library.cli",
}

# Old service names that keep working.
ALIASES = {"papers": "library"}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ALIASES:
        argv = [ALIASES[argv[0]], *argv[1:]]
    if argv and argv[0] in SERVICES:
        service = importlib.import_module(SERVICES[argv[0]])
        service.main(argv[1:], prog=f"uu {argv[0]}")
        return

    p = argparse.ArgumentParser(
        prog="uu",
        description="Headless access to Utrecht University platforms.",
        epilog="Run 'uu <service> --help' for the commands of a service.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("service", choices=sorted(SERVICES))
    p.parse_args(argv)
