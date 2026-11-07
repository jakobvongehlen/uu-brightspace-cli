"""Brightspace commands that read course data: login, courses, toc, assignments, grades, schedule, deadlines."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import BrowserContext

from uu_cli import paths
from uu_cli.paths import safe_filename
from uu_cli.services.brightspace.api import BASE_URL, api_fetch, get_token
from uu_cli.services.brightspace.sheets import (
    download_google_sheet, extract_google_sheets_urls, parse_sheet_deadlines,
)


def cmd_login(ctx: BrowserContext, args) -> int:
    """Log in, unless the saved session still is, and cache a new token."""
    if not get_token(ctx, args, session=True):
        return 1
    print("Logged in to Brightspace. Token cached.", file=sys.stderr)
    return 0


def cmd_courses(ctx: BrowserContext, args) -> int:
    token = get_token(ctx, args)
    if not token:
        return 1
    data = api_fetch(ctx,
        f"{BASE_URL}/d2l/api/lp/1.43/enrollments/myenrollments/", token)
    items = (data.get("Items", []) if isinstance(data, dict) else data) or []
    print(f"\n{'Code':<12} {'Name':<45} {'OrgUnitId'}")
    print("-" * 75)
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("OrgUnit", {}).get("Code", "") or ""
        name = (item.get("OrgUnit", {}).get("Name", "") or "")[:44]
        ouid = item.get("OrgUnit", {}).get("Id", "") or ""
        print(f"{code:<12} {name:<45} {ouid}")
    return 0


def cmd_toc(ctx: BrowserContext, args) -> int:
    token = get_token(ctx, args)
    if not token:
        return 1
    ou = args.org_unit
    data = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/content/toc", token)
    print(json.dumps(data, indent=2))
    return 0


def cmd_assignments(ctx: BrowserContext, args) -> int:
    """List assignments — from dropbox API + TOC LTI quick-links."""
    token = get_token(ctx, args)
    if not token:
        return 1
    ou = args.org_unit

    results: list[dict] = []

    # 1. Dropbox folders (native Brightspace assignments)
    data = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/dropbox/folders/", token)
    folders = (data.get("Folders", []) if isinstance(data, dict)
               else (data if isinstance(data, list) else []))
    for f in folders:
        if not isinstance(f, dict):
            continue
        results.append({
            "source": "dropbox",
            "id": f.get("Id"),
            "name": f.get("Name", "?"),
            "due_date": f.get("DueDate") or f.get("EndDate") or "",
            "start_date": f.get("StartDate", "") or "",
            "type": "Assignment",
        })

    # 2. TOC topics — detect LTI assignment links (FeedbackFruits, etc.)
    toc = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/content/toc", token)
    modules = (toc.get("Modules", []) if isinstance(toc, dict) else [])
    for mod in modules:
        for topic in mod.get("Topics", []):
            if not isinstance(topic, dict):
                continue
            t_url = topic.get("Url", "") or ""
            t_type = topic.get("TypeIdentifier", "") or ""
            # LTI links are external tools (Assignments, FeedbackFruits, etc.)
            if t_type == "LTI" and ("assignment" in t_url.lower() or
                                     "dropbox" in t_url.lower() or
                                     "feedbackfruits" in t_url.lower() or
                                     "peer" in t_url.lower()):
                results.append({
                    "source": "lti",
                    "id": topic.get("Identifier") or topic.get("TopicId"),
                    "name": topic.get("Title", "LTI Assignment"),
                    "due_date": "",
                    "start_date": "",
                    "type": "LTI",
                    "url": t_url,
                })

    if not results:
        print("No assignments found.")
        return 0

    print(f"\n{'Name':<50} {'Due Date':<25} {'Type'}")
    print("-" * 90)
    for a in results:
        due = (a["due_date"][:10] if a["due_date"] else "—")
        print(f"{a['name']:<50} {due:<25} {a['type']} ({a['source']})")
    return 0


def cmd_grades(ctx: BrowserContext, args) -> int:
    token = get_token(ctx, args)
    if not token:
        return 1
    ou = args.org_unit
    data = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/grades/values/myGradeValues/", token)
    items = (data.get("Items", []) if isinstance(data, dict)
             else (data if isinstance(data, list) else []))
    for item in items:
        if not isinstance(item, dict):
            print(f"  {item}")
            continue
        print(f"  {item.get('DisplayName','?'):<40} "
              f"{item.get('WeightedNumerator','?')}/{item.get('WeightedDenominator','?')}")
    return 0


def cmd_schedule(ctx: BrowserContext, args) -> int:
    """
    Detect Google Sheets URLs in course content and download them as CSV.
    Searches module descriptions and topic titles from the TOC.
    """
    token = get_token(ctx, args)
    if not token:
        return 1
    ou = args.org_unit

    toc = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/content/toc", token)
    modules = (toc.get("Modules", []) if isinstance(toc, dict) else [])

    sheets_urls: dict[str, str] = {}  # label -> csv_url

    for mod in modules:
        if not isinstance(mod, dict):
            continue
        # Check module title and description
        for field in ("Title", "Description"):
            val = mod.get(field, "")
            if isinstance(val, dict):
                val = val.get("Html", val.get("Text", ""))
            if val:
                for url in extract_google_sheets_urls(str(val)):
                    sheets_urls[f"{mod.get('Title','module')}_{field}"] = url
        # Check topics
        for topic in mod.get("Topics", []):
            if not isinstance(topic, dict):
                continue
            for field in ("Title", "Description"):
                val = topic.get(field, "")
                if isinstance(val, dict):
                    val = val.get("Html", val.get("Text", ""))
                if val:
                    for url in extract_google_sheets_urls(str(val)):
                        key = topic.get("Title", "topic")
                        sheets_urls[key] = url

    if not sheets_urls:
        print("No Google Sheets URLs found in course content.")
        return 0

    print(f"Found {len(sheets_urls)} Google Sheets:\n")
    base_dir = Path(args.output or
                    str(paths.OUTPUT_DIR / ou))
    base_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    for label, csv_url in sheets_urls.items():
        safe = label.replace("/", "_").replace("\\", "_")[:100]
        dest = base_dir / f"{safe}.csv"
        if download_google_sheet(csv_url, dest, label=label):
            downloaded += 1
            # Try to parse and display deadline rows
            deadlines = parse_sheet_deadlines(dest)
            if deadlines:
                print(f"  Deadlines found in '{label}':")
                for d in deadlines[:10]:
                    print(f"    {d['date']}  {d['title']}")
                if len(deadlines) > 10:
                    print(f"    ... ({len(deadlines)-10} more)")
                print()

    print(f"\nDownloaded {downloaded}/{len(sheets_urls)} sheets → {base_dir}")
    return 0


def cmd_deadlines(ctx: BrowserContext, args) -> int:
    """
    Show all upcoming deadlines from:
      1. Brightspace dropbox assignments (native due dates)
      2. Google Sheets schedule (detected from course content)
    Merged and sorted by date.
    """
    token = get_token(ctx, args)
    if not token:
        return 1
    ou = args.org_unit

    events: list[dict] = []

    # 1. Dropbox due dates
    data = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/dropbox/folders/", token)
    folders = (data.get("Folders", []) if isinstance(data, dict)
               else (data if isinstance(data, list) else []))
    for f in folders:
        if not isinstance(f, dict):
            continue
        due = (f.get("DueDate") or f.get("EndDate") or "")[:10]
        if due:
            events.append({"date": due, "title": f.get("Name", "?"), "source": "Brightspace"})
        start = (f.get("StartDate", "") or "")[:10]
        if start:
            events.append({"date": start, "title": f.get("Name", "?") + " (opens)",
                           "source": "Brightspace"})

    # 2. Google Sheets schedule
    toc = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/content/toc", token)
    modules = (toc.get("Modules", []) if isinstance(toc, dict) else [])
    base_dir = Path(str(paths.OUTPUT_DIR / ou))
    for mod in modules:
        if not isinstance(mod, dict):
            continue
        for field in ("Title", "Description"):
            val = mod.get(field, "")
            if isinstance(val, dict):
                val = val.get("Html", val.get("Text", ""))
            csv_urls = extract_google_sheets_urls(str(val))
            if not csv_urls:
                continue
            safe = safe_filename(mod.get("Title", "schedule"))[:80]
            # Try both the field-qualified name (from cmd_schedule) and bare name
            for fname in (f"{safe}_Description", safe, f"{safe}_Title"):
                dest = base_dir / f"{fname}.csv"
                if dest.exists():
                    break
            if download_google_sheet(csv_urls[0], dest):
                deadlines = parse_sheet_deadlines(dest)
                for d in deadlines:
                    d["source"] = "Schedule"
                    events.append(d)

    # Sort by date
    events.sort(key=lambda e: (e["date"], e["title"]))

    if not events:
        print("No deadlines found.")
        return 0

    print(f"\n{'Date':<12} {'Event':<50} {'Source'}")
    print("-" * 85)
    today = time.strftime("%Y-%m-%d")
    for e in events:
        marker = " ← TODAY" if e["date"] == today else ""
        print(f"{e['date']:<12} {e['title'][:48]:<50} {e['source']}{marker}")
    return 0
