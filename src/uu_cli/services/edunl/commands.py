"""edu.nl commands: login, shorten, links."""

from __future__ import annotations

import json
import sys

from playwright.sync_api import BrowserContext

from uu_cli import login
from uu_cli.login import Site, ensure_login
from uu_cli.services.edunl.api import MANAGE_URL, EdunlError, NotLoggedIn, links, logged_in, set_label, shorten

SITE = Site("edu.nl", MANAGE_URL, logged_in)


def cmd_login(ctx: BrowserContext, args) -> int:
    return login.cmd_login(ctx, args, SITE)


def cmd_shorten(ctx: BrowserContext, args) -> int:
    """Make a short link; prints only the link on stdout, so a script can take it."""
    if not args.url.lower().startswith(("http://", "https://")):
        print(f"ERROR: Not a web address: {args.url} (it starts with http:// or https://).",
              file=sys.stderr)
        return 2
    if not ensure_login(ctx, args, SITE):
        return 1
    try:
        short, page = shorten(ctx, args.url)
        print(short)
        if args.label:
            set_label(page, args.label)
            print(f"[EDU.NL] Label: {args.label}", file=sys.stderr)
    except EdunlError as err:
        print(f"ERROR: edu.nl: {err}", file=sys.stderr)
        return 1
    return 0


def cmd_links(ctx: BrowserContext, args) -> int:
    """Your links, newest first, one page of the overview at a time."""
    try:
        rows, more = links(ctx, args.page, args.search)
    except NotLoggedIn:
        if not ensure_login(ctx, args, SITE):
            return 1
        rows, more = links(ctx, args.page, args.search)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No links found.")
        return 0
    for r in rows:
        label = f"  [{r['label']}]" if r["label"] else ""
        print(f"{r['short']:<24} {r['date']:<10} {r['clicks']:>5} clicks  {r['url']}{label}")
    if more:
        print(f"\nMore on page {args.page + 1} (--page {args.page + 1}).")
    return 0
