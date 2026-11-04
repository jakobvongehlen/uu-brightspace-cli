"""Osiris Student logged in: your grades and the courses you're enrolled in (read only).

Logged out, osiris-student.uu.nl sends you through Osiris' auth server (osi-auth-server-prd.
osiris-link.nl) to UU's Solis-id page, with a code, and back.
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext

from uu_cli import login
from uu_cli.login import Site
from uu_cli.services.osiris.api import BASE_URL

# No way to tell yet whether you're logged in: a login that ends back on Osiris counts.
SITE = Site("Osiris", f"{BASE_URL}/")


def cmd_login(ctx: BrowserContext, args) -> int:
    return login.cmd_login(ctx, args, SITE)
