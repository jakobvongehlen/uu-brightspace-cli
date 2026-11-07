"""Announcements: one course's (--org-unit), or every course's in one browser (--all-courses)."""

from __future__ import annotations

import json
import sys
from datetime import date
from typing import Optional

from playwright.sync_api import BrowserContext

from uu_cli.services.brightspace.api import BASE_URL, api_fetch, api_get, enrolled_courses, get_token
from uu_cli.services.brightspace.discussions import html_to_text


def since_date(value: str) -> str:
    """--since: a date as YYYY-MM-DD (argparse says so, and exits 2, for anything else)."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(value) from None


since_date.__name__ = "date (YYYY-MM-DD)"  # what argparse's error message calls it


def cmd_news(ctx: BrowserContext, args) -> int:
    """Show announcements: one course's, or with --all-courses every course's."""
    token = get_token(ctx, args)
    if not token:
        return 1
    if args.all_courses:
        return _all_courses(ctx, token, args)
    ou = args.org_unit
    raw = fetch_news(ctx, token, ou)
    if isinstance(raw, str):
        print(f"ERROR: Announcements of {ou}: {raw}", file=sys.stderr)
        return 1
    raw = [r for r in raw if _wanted(r, args.since)]
    if args.json:
        course = _course_label(ou, _enrollment(ctx, token, ou))
        print(json.dumps(_newest_first([(r, news_item(r, ou, course)) for r in raw]), indent=2,
                         ensure_ascii=False))
        return 0
    if not raw:
        print("No announcements found.")
        return 0
    for item in raw:
        print_raw(item)
    return 0


def _all_courses(ctx: BrowserContext, token: str, args) -> int:
    """Every enrolled course's announcements, with the one token and browser. A course whose
    announcements can't be read is listed under "failed" and the rest still printed: exit 0."""
    courses = enrolled_courses(ctx, token)
    if courses is None:
        print("ERROR: Enrollment fetch failed or returned invalid data.", file=sys.stderr)
        return 1
    items: list[tuple[dict, dict]] = []
    failed: dict[str, str] = {}
    shown: list[tuple[str, list[dict]]] = []
    for ou, org in courses:
        try:
            raw = fetch_news(ctx, token, ou)
        except Exception as e:  # one course's answer broke something: the others may still work
            raw = f"{type(e).__name__}: {e}"
        if isinstance(raw, str):
            failed[ou] = raw
            print(f"[NEWS] {ou}: {raw}", file=sys.stderr)
            continue
        raw = [r for r in raw if _wanted(r, args.since)]
        course = _course_label(ou, org)
        items += [(r, news_item(r, ou, course)) for r in raw]
        if raw:
            shown.append((course, raw))
    if args.json:
        out = {"since": args.since, "courses": len(courses), "items": _newest_first(items),
               "failed": failed}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    for course, raw in shown:
        print(f"── {course}\n")
        for item in raw:
            print_raw(item)
    if not shown:
        print("No announcements found.")
    if failed:
        print(f"Could not read the announcements of {len(failed)} course(s): {', '.join(failed)}.")
    return 0


def fetch_news(ctx: BrowserContext, token: str, ou: str) -> list[dict] | str:
    """The course's announcements as the API gives them, or why they couldn't be read."""
    status, data = api_get(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/news/", token)
    if status == 0:
        return "the request failed"
    if status >= 400:
        errors = data.get("Errors") if isinstance(data, dict) else None
        first = errors[0] if isinstance(errors, list) and errors else None
        why = first.get("Message") if isinstance(first, dict) else None
        return f"Brightspace answered {status}" + (f": {why}" if why else "")
    if isinstance(data, dict):  # the route has answered with a dict holding the list, too
        data = data.get("Items", data.get("News", []))
    if data is None:  # an empty answer: nothing to show
        return []
    if not isinstance(data, list):
        return "Brightspace's answer was not a list of announcements"
    return [item for item in data if isinstance(item, dict)]


def news_item(raw: dict, ou: str, course: str) -> dict:
    """One announcement for --json: its course, date (YYYY-MM-DD), title, full text and attachments."""
    return {
        "org_unit": ou,
        "course": course,
        "date": _date(raw) or None,
        "title": raw.get("Title") or "untitled",
        "text": _text(raw.get("Body")),
        "attachments": _attachments(raw),
    }


def print_raw(item: dict) -> None:
    """An announcement as `news` has always printed it: ★ (published) or ○, date and title, the
    text's start and the attachments' names."""
    star = "★" if item.get("IsPublished") else "○"
    title = item.get("Title", "untitled")
    body_raw = item.get("Body", {})
    if isinstance(body_raw, dict):
        text = body_raw.get("Text", body_raw.get("Html", ""))[:300]
    else:
        text = str(body_raw)[:300]
    print(f"{star} [{_date(item)}] {title}")
    if text:
        print(f"   {text}")
    names = _attachments(item)
    if names:
        print(f"   📎 Attachments: {', '.join(names)}")
    print()


def _date(raw: dict) -> str:
    return (raw.get("CreatedDate", "") or "")[:10]


def _wanted(raw: dict, since: Optional[str]) -> bool:
    """On or after --since, by the date shown. Without --since, everything."""
    return not since or _date(raw) >= since


def _text(body) -> str:
    if not isinstance(body, dict):
        return str(body or "").strip()
    return html_to_text(body.get("Html") or "") or (body.get("Text") or "").strip()


def _attachments(raw: dict) -> list[str]:
    atts = raw.get("Attachments")
    if not isinstance(atts, list):
        return []
    return [a.get("FileName", "?") for a in atts if isinstance(a, dict)]


def _newest_first(pairs: list[tuple[dict, dict]]) -> list[dict]:
    """The --json items of (raw, item) pairs, newest first by when each was created."""
    return [item for _, item in sorted(pairs, key=lambda p: p[0].get("CreatedDate") or "", reverse=True)]


def _enrollment(ctx: BrowserContext, token: str, ou: str) -> dict:
    data = api_fetch(ctx, f"{BASE_URL}/d2l/api/lp/1.43/enrollments/myenrollments/{ou}", token)
    org = data.get("OrgUnit") if isinstance(data, dict) else None
    return org if isinstance(org, dict) else {}


def _course_label(ou: str, org: dict) -> str:
    """"<code> <name>", as `sync-all --dry-run` lists the course; the org-unit id without either."""
    return " ".join(p for p in (org.get("Code") or "", org.get("Name") or "") if p) or ou
