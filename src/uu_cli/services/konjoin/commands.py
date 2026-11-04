"""Konjoin commands: login, projects, project, export."""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import BrowserContext

from uu_cli import login
from uu_cli.login import Site, ensure_login
from uu_cli.services.konjoin.api import (
    BASE_URL, LOGIN_URL, NotLoggedIn, current_user, get_json, project_pages,
)

# Konjoin shows every logged-in user these for every person (project leaders' emails even to
# guests). A project overview doesn't need them, and a file with everyone's email address and
# organisation id is a list of personal data, so JSON output leaves them out unless asked.
CONTACT_FIELDS = ("email", "organization_uid")


def without_contact_details(data):
    """data with every CONTACT_FIELDS key removed, at any depth."""
    if isinstance(data, dict):
        return {k: without_contact_details(v) for k, v in data.items() if k not in CONTACT_FIELDS}
    if isinstance(data, list):
        return [without_contact_details(v) for v in data]
    return data


def _for_output(data, args):
    return data if getattr(args, "keep_contact_details", False) else without_contact_details(data)


SITE = Site("Konjoin", LOGIN_URL, current_user)


def cmd_login(ctx: BrowserContext, args) -> int:
    return login.cmd_login(ctx, args, SITE)


def cmd_projects(ctx: BrowserContext, args) -> int:
    """List projects. Public projects need no login; a saved session is used when there is one."""
    projects, last = [], {}
    for last in project_pages(ctx, args.search, args.page, args.all):
        projects += last.get("data", [])
    if args.json:
        print(json.dumps(_for_output(projects, args), indent=2, ensure_ascii=False))
        return 0
    if not projects:
        print("No projects found.")
        return 0
    for p in projects:
        print(f"{_label(p.get('status')):<8} {p.get('label', '?')}")
        print(f"{'':<8} {p.get('slug', '')} · {_label(p.get('institute'))}")
    if args.all:
        print(f"\n{len(projects)} of {last.get('total', len(projects))} projects")
    else:
        print(f"\nPage {last.get('current_page', args.page)} of {last.get('last_page', '?')}"
              f" · {last.get('total', '?')} projects (--page N, or --all)")
    return 0


def cmd_project(ctx: BrowserContext, args) -> int:
    """Show one project; Konjoin only shows project details to logged-in users."""
    try:
        project = get_json(ctx, f"/api/project/{args.slug}")
    except NotLoggedIn:
        if not ensure_login(ctx, args, SITE):
            return 1
        project = get_json(ctx, f"/api/project/{args.slug}")
    if args.json:
        print(json.dumps(_for_output(project, args), indent=2, ensure_ascii=False))
        return 0
    print(project.get("label", args.slug))
    print(f"{BASE_URL}/project/{project.get('slug', args.slug)}\n")
    rows = [
        ("Status", _label(project.get("status"))),
        ("Department", _label(project.get("institute"))),
        ("Leader", (project.get("leader") or {}).get("full_name", "")),
        ("Collaborators", ", ".join(c.get("full_name", "") for c in project.get("collaborators") or [])),
        ("Studies", ", ".join(_label(s) for s in project.get("studies") or [])),
        ("Types", ", ".join(_label(t) for t in project.get("types") or [])),
        ("Created", (project.get("created_at") or "")[:10]),
        ("Closes", (project.get("close_at") or "")[:10] or "—"),
    ]
    for name, value in rows:
        if value:
            print(f"{name + ':':<15}{value}")
    description = html_to_text(project.get("description") or "")
    if description:
        print(f"\n{description}")
    return 0


def cmd_export(ctx: BrowserContext, args) -> int:
    """Save every project (all pages) to a JSON file."""
    projects = []
    for page in project_pages(ctx, args.search, all_pages=True):
        projects += page.get("data", [])
        print(f"  page {page.get('current_page')} of {page.get('last_page')}", file=sys.stderr)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_for_output(projects, args), indent=2, ensure_ascii=False))
    kept = "" if args.keep_contact_details else " (without emails and organisation ids)"
    print(f"{len(projects)} projects → {out}{kept}")
    return 0


def html_to_text(fragment: str) -> str:
    """Konjoin descriptions are HTML; keep paragraphs and list items, drop the tags."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", fragment)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _label(obj) -> str:
    return (obj or {}).get("label", "") if isinstance(obj, dict) else str(obj or "")
