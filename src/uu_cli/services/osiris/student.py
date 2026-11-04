"""Osiris Student logged in: your grades and the courses you're enrolled in (read only).

Logged out, osiris-student.uu.nl sends you through Osiris' auth server (osi-auth-server-prd.
osiris-link.nl) to UU's Solis-id page, with a code, and back.
"""

from __future__ import annotations

import sys

from playwright.sync_api import BrowserContext

from uu_cli import auth
from uu_cli.login import log_in, single_sign_on
from uu_cli.services.osiris.api import BASE_URL

START_URL = f"{BASE_URL}/"


def ensure_login(ctx: BrowserContext, args) -> bool:
    """Log in to Osiris Student unless already in; an existing UU session may need no code."""
    if single_sign_on(ctx, START_URL):
        return True
    if not auth.ready_to_login(args):
        print(f"ERROR: Cannot log in: {auth.CANNOT_LOGIN}.", file=sys.stderr)
        return False
    log_in(ctx, START_URL, args, name="Osiris")
    return True
