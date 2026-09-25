#!/usr/bin/env python3
"""Headless Brightspace course-sync CLI built on Playwright."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import csv

import pyotp
import requests
from playwright.sync_api import sync_playwright, BrowserContext, Page

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).parent
SESSION_DIR = Path(os.environ.get("BRIGHTSPACE_SESSION_DIR",
                         str(SCRIPT_DIR.parent / "brightspace" / "session")))
TOKEN_FILE = Path(os.environ.get("BRIGHTSPACE_TOKEN_FILE",
                         str(SCRIPT_DIR.parent / "brightspace" / "token.json")))
SESSION_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Auth constants ─────────────────────────────────────────────────────────────
BASE_URL  = os.environ.get("BRIGHTSPACE_BASE_URL", "https://uu.brightspace.com").rstrip("/")

# ── Helpers ───────────────────────────────────────────────────────────────────
def totp(secret: str) -> str:
    return pyotp.TOTP(secret).now()

def load_token() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None

def save_token(token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "token": token,
        "savedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiresIn": 28800,
    }))

def extract_token(ctx: BrowserContext) -> str | None:
    """
    Pull D2L.Fetch.Tokens from localStorage via Playwright.
    Handles cross-origin restrictions (skip pages where localStorage is inaccessible).
    """
    for page in ctx.pages:
        try:
            raw = page.evaluate(
                "localStorage.getItem('D2L.Fetch.Tokens') || ''"
            )
        except Exception:
            continue
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for v in parsed.values():
                    if isinstance(v, dict) and v.get("access_token"):
                        return v["access_token"]
                if parsed.get("access_token"):
                    return parsed["access_token"]
        except Exception:
            pass
    return None

def extract_google_sheets_urls(text: str) -> list[str]:
    """Extract Google Sheets URLs from any text (HTML, plain text, JSON)."""
    urls = []
    for match in re.finditer(
        r"https://docs\.google\.com/spreadsheets/d/[\w\-]+/edit[^\s\)\]\}\"]*",
        text
    ):
        url = match.group(0).rstrip("/")
        # Convert edit URL to export CSV URL
        spreadsheet_id = url.split("/d/")[1].split("/")[0] if "/d/" in url else None
        if spreadsheet_id:
            export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid=0"
            urls.append(export_url)
    return urls

def download_google_sheet(csv_url: str, dest: Path, label: str = "") -> bool:
    """Download a Google Sheet as CSV. Returns True on success."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(csv_url, timeout=30)
        if resp.ok and len(resp.content) > 50:
            dest.write_bytes(resp.content)
            print(f"  ✓ {label or dest.name}", file=sys.stderr)
            return True
        else:
            print(f"  ! {label}: HTTP {resp.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  ! {label}: {e}", file=sys.stderr)
    return False

def parse_sheet_deadlines(csv_path: Path) -> list[dict]:
    """Read the schedule CSV and return events as (date, title) rows.

    Priority: Deadline column > Topic column > first non-empty cell in the row.
    Dates are expected in the 'Date' column (e.g. "April 21, 2026Tue").
    """
    deadlines = []
    month_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                 "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    try:
        with open(csv_path, encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                date_str = row.get("Date", "").strip()
                if not date_str:
                    continue
                # Parse "Month Day, Year[DayOfWeek]" e.g. "April 21, 2026Tue"
                m = re.search(
                    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})",
                    date_str, re.I)
                if not m:
                    continue
                mon, day, year = m.groups()
                month = month_map.get(mon[:3].lower(), 1)
                iso = f"{year}-{month:02d}-{int(day):02d}"

                # Title priority: explicit Deadline field > Topic > first non-empty cell
                title = row.get("Deadline", "").strip()
                if not title:
                    title = row.get("Topic", "").strip().split("\n")[0]
                if not title:
                    for v in row.values():
                        if v.strip() and v.strip() != date_str:
                            title = v.strip().split("\n")[0]
                            break

                deadlines.append({"date": iso, "title": title, "raw": date_str})
    except Exception as e:
        print(f"  ! Failed to parse {csv_path}: {e}", file=sys.stderr)
    return deadlines


def _safe(s: str | Path) -> str:
    """Sanitize a string for use as a filename — removes filesystem-unsafe chars."""
    return str(s or "untitled").replace("\\", "_").replace("\n", " ").strip()[:180]


# ── Login ─────────────────────────────────────────────────────────────────────
def do_login(ctx: BrowserContext, solisid: str, password: str,
             totp_secret: str, headless: bool = True) -> None:
    if not solisid:
        print("ERROR: --solisid or BRIGHTSPACE_SOLISID required for login",
              file=sys.stderr)
        sys.exit(1)

    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    print("[AUTH] Navigating to Brightspace...", file=sys.stderr)
    page.goto(BASE_URL, wait_until="load", timeout=30_000)
    page.wait_for_selector("#Ecom_User_ID", timeout=20_000)
    print(f"[AUTH] At NISP form: {page.url}", file=sys.stderr)

    print("[AUTH] Filling credentials...", file=sys.stderr)
    page.fill("#Ecom_User_ID", solisid)
    page.fill("#Ecom_Password", password)
    page.click("#loginButton2")

    print("[AUTH] Waiting for OTP form...", file=sys.stderr)
    page.wait_for_timeout(3000)
    print(f"[AUTH] URL: {page.url}", file=sys.stderr)

    otp_sel = page.evaluate(
        "() => {"
        "  const el = document.querySelector("
        "    'input[autocomplete=\"one-time-code\"],"
        "    input[autocomplete=\"one-time-password\"],"
        "    input[autocomplete=\"otp\"],"
        "    input[maxlength=\"6\"],"
        "    input[type=\"text\"]'"
        "  );"
        "  if (el && el.id) return '#' + el.id;"
        "  if (el && el.name) return 'input[name=\"' + el.name + '\"]';"
        "  return null;"
        "}"
    )

    if not otp_sel:
        page.screenshot(path=str(SESSION_DIR / "debug_otp_missing.png"))
        with open(str(SESSION_DIR / "debug_otp_missing.html"), "w") as f:
            f.write(page.content())
        body_text = page.inner_text("body")[:300]
        print(f"[AUTH] OTP not found. Body: {body_text}", file=sys.stderr)
        raise RuntimeError(f"OTP field not found. URL={page.url}")

    print(f"[AUTH] OTP field found: {otp_sel}", file=sys.stderr)

    code = totp(totp_secret)
    page.fill(otp_sel, code)

    button_selectors = ["#loginButton2", "button[type='submit']",
                        "input[type='submit']", "button"]
    btn_sel = None
    for sel in button_selectors:
        if page.locator(sel).count() > 0:
            btn_sel = sel
            break

    if btn_sel:
        page.click(btn_sel)
    else:
        page.keyboard.press("Enter")

    print("[AUTH] Waiting for Brightspace...", file=sys.stderr)
    page.wait_for_timeout(5000)
    print(f"[AUTH] URL: {page.url}", file=sys.stderr)

    if urlparse(page.url).hostname != urlparse(BASE_URL).hostname:
        page.screenshot(path=str(SESSION_DIR / "debug_not_brightspace.png"))
        with open(str(SESSION_DIR / "debug_not_brightspace.html"), "w") as f:
            f.write(page.content())
        body_text = page.inner_text("body")[:300]
        raise RuntimeError(f"Post-OTP URL is not Brightspace: {page.url}\nBody: {body_text}")

    print(f"[AUTH] ✓ Logged in: {page.url}", file=sys.stderr)
    page.wait_for_timeout(2000)

# ── Authenticated fetch helper ─────────────────────────────────────────────────
def api_fetch(ctx: BrowserContext, url: str, token: str,
              timeout: float = 20000) -> Optional[Any]:
    """GET a Brightspace /d2l/api/ endpoint via Playwright page JS (carries cookies)."""
    page = ctx.pages[0]
    try:
        data = page.evaluate(f"""
            async () => {{
                const r = await fetch(
                    "{url}",
                    {{ headers: {{ "Authorization": "Bearer {token}",
                                   "Accept": "application/json" }} }}
                );
                const text = await r.text();
                if (r.status === 204 || !text.trim()) return null;
                try {{
                    return JSON.parse(text);
                }} catch {{
                    return null;
                }}
            }}
        """)
        return data
    except Exception as e:
        print(f"  ! API {url}: {e}", file=sys.stderr)
        return None

# ── Commands ───────────────────────────────────────────────────────────────────
def cmd_login(ctx: BrowserContext, args) -> int:
    token = extract_token(ctx)
    if not token:
        do_login(ctx, args.solisid, args.password, args.totp)
        page = ctx.pages[0]
        page.goto(f"{BASE_URL}/d2l/home", wait_until="load", timeout=15000)
        page.wait_for_timeout(2000)
        token = extract_token(ctx)
    if token:
        save_token(token)
        print("Token cached.", file=sys.stderr)
    return 0

def cmd_courses(ctx: BrowserContext, args) -> int:
    token = _get_token(ctx, args)
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
    token = _get_token(ctx, args)
    if not token:
        return 1
    ou = args.org_unit
    data = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/content/toc", token)
    print(json.dumps(data, indent=2))
    return 0

def cmd_assignments(ctx: BrowserContext, args) -> int:
    """List assignments — from dropbox API + TOC LTI quick-links."""
    token = _get_token(ctx, args)
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
    token = _get_token(ctx, args)
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

def cmd_news(ctx: BrowserContext, args) -> int:
    """Fetch and display announcements. Handles both list and dict responses."""
    token = _get_token(ctx, args)
    if not token:
        return 1
    ou = args.org_unit
    data = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/news/", token)

    # Guard: news API can return a dict with Items or a raw list
    items: list = []
    if data is None:
        pass
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("Items", data.get("News", []))

    if not items:
        print("No announcements found.")
        return 0

    for item in items:
        if not isinstance(item, dict):
            print(f"  [raw] {item}")
            continue
        star = "★" if item.get("IsPublished") else "○"
        title = item.get("Title", "untitled")
        body_raw = item.get("Body", {})
        if isinstance(body_raw, dict):
            text = body_raw.get("Text", body_raw.get("Html", ""))[:300]
        else:
            text = str(body_raw)[:300]
        date = (item.get("CreatedDate", "") or "")[:10]
        print(f"{star} [{date}] {title}")
        if text:
            print(f"   {text}")
        atts = item.get("Attachments")
        if atts and isinstance(atts, list):
            names = ", ".join(
                a.get("FileName", "?") for a in atts
                if isinstance(a, dict)
            )
            print(f"   📎 Attachments: {names}")
        print()
    return 0

def cmd_schedule(ctx: BrowserContext, args) -> int:
    """
    Detect Google Sheets URLs in course content and download them as CSV.
    Searches module descriptions and topic titles from the TOC.
    """
    token = _get_token(ctx, args)
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
                    str(SCRIPT_DIR.parent / "brightspace_sync" / ou))
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
    token = _get_token(ctx, args)
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
    base_dir = Path(str(SCRIPT_DIR.parent / "brightspace_sync" / ou))
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
            safe = _safe(mod.get("Title", "schedule"))[:80]
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

def cmd_sync(ctx: BrowserContext, args) -> int:
    """Download ALL course content files to a local directory."""
    if not args.password or not args.totp:
        args.password = os.environ.get("BRIGHTSPACE_PASSWORD", "")
        args.totp     = os.environ.get("BRIGHTSPACE_TOTP_SECRET", "")

    # ── Establish an authenticated page context first ──────────────────────
    # A cached bearer token authenticates /d2l/api/* but NOT /content/enforced/*
    # content pages, which need SAML session cookies in this browser process.
    # Chrome does not persist those session cookies to the profile dir, so an
    # offline cached token alone yields "auth redirect" on every content page and
    # a metadata-only sync. Always probe the live page context first.
    token = ""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    session_ok = False
    try:
        page.goto(f"{BASE_URL}/d2l/home", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        if urlparse(page.url).hostname == urlparse(BASE_URL).hostname:
            session_ok = True
    except Exception:
        pass

    cached = load_token()
    if session_ok:
        fresh_token = extract_token(ctx)
        if fresh_token:
            save_token(fresh_token)
        token = fresh_token or (cached or {}).get("token", "")
        if token:
            print("[SYNC] Reusing authenticated browser session.", file=sys.stderr)

    if not token:
        if not args.password or not args.totp:
            print("ERROR: --password and --totp required for sync (no authenticated session).", file=sys.stderr)
            return 1
        print("[SYNC] Session expired — fresh login...", file=sys.stderr)
        do_login(ctx, args.solisid, args.password, args.totp)
        # Do not re-navigate — do_login already lands on /d2l/home
        page.wait_for_timeout(2000)

        fresh_token = extract_token(ctx)
        if fresh_token:
            save_token(fresh_token)
        token = fresh_token or (load_token() or {}).get("token", "")
        if not token:
            print("[SYNC] Token extraction failed.", file=sys.stderr)
            return 1
    print(f"[SYNC] Session ready.", file=sys.stderr)

    ou = args.org_unit
    base_dir = Path(args.output or
                    str(SCRIPT_DIR.parent / "brightspace_sync")).resolve()
    out_dir = base_dir / ou
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── helpers ────────────────────────────────────────────────────────────────
    def _ext(url: str) -> str:
        p = url.partition("?")[0].lower()
        for e in (".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx",
                  ".xls", ".zip", ".mp4", ".mp3", ".png", ".jpg",
                  ".jpeg", ".gif", ".svg", ".html", ".htm"):
            if p.endswith(e):
                return e
        return ""

    dl_page: Optional[Page] = None

    def _ensure_dl_page() -> Page:
        nonlocal dl_page
        if dl_page is None:
            dl_page = ctx.new_page()
            dl_page.set_viewport_size({"width": 1280, "height": 720})
        return dl_page

    n_files = 0
    skipped = 0

    def _download(url: str, dest: Path, label: str = "") -> bool:
        nonlocal n_files, skipped, dl_page
        if dest.exists():
            skipped += 1
            return True
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            ext = _ext(url)
            is_html = ext in (".html", ".htm", ".xhtml")
            pg = _ensure_dl_page()

            if is_html:
                resp = pg.goto(url, wait_until="domcontentloaded", timeout=25000)
                if resp is None or not resp.ok:
                    print(f"  ✗ {label}: HTTP {resp.status if resp else 'none'}", file=sys.stderr)
                    return False
                if "login" in pg.url.lower() or "surfconext" in pg.url.lower():
                    print(f"  ⊘ {label}: auth redirect", file=sys.stderr)
                    return False
                html = pg.content()
                if len(html) < 500 and "login" in html.lower():
                    print(f"  ⊘ {label}: auth redirect", file=sys.stderr)
                    return False
                dest.write_text(html)
                n_files += 1
                print(f"  ✓ {label or dest.name}"[:120], file=sys.stderr)
                return True

            else:
                dl_triggered = False
                dl_error = None
                try:
                    with pg.expect_download(timeout=25000) as dl_ctx:
                        try:
                            pg.goto(url, wait_until="commit", timeout=20000)
                            pg.wait_for_timeout(2000)
                        except Exception as nav_err:
                            dl_error = str(nav_err)
                    dl = dl_ctx.value
                    dl.save_as(str(dest))
                    dl_triggered = True
                except Exception as e:
                    dl_error = str(e)

                if not dl_triggered:
                    try:
                        resp2 = pg.goto(url, wait_until="domcontentloaded", timeout=20000)
                    except Exception as nav_err2:
                        dl_error2 = str(nav_err2)
                        if "download" in dl_error2.lower():
                            print(f"  ~ {label}: download started but not captured", file=sys.stderr)
                            return False
                        resp2 = None
                    if resp2 and resp2.ok:
                        if "login" in pg.url.lower() or "surfconext" in pg.url.lower():
                            print(f"  ⊘ {label}: auth redirect", file=sys.stderr)
                            return False
                        html = pg.content()
                        if len(html) < 500 and "login" in html.lower():
                            print(f"  ⊘ {label}: auth redirect", file=sys.stderr)
                            return False
                        dest.write_text(html)
                        print(f"  ~ {label}: inline content saved", file=sys.stderr)
                        n_files += 1
                        return True
                    else:
                        err_msg = dl_error or dl_error2 if 'dl_error2' in dir() else "unknown"
                        print(f"  ✗ {label}: {err_msg[:80]}", file=sys.stderr)
                        return False
                n_files += 1
                print(f"  ✓ {label or dest.name}"[:120], file=sys.stderr)
                return True

        except Exception as e:
            print(f"  ✗ {label}: {e}"[:120], file=sys.stderr)
            return False

    # ── 1. TOC ────────────────────────────────────────────────────────────────
    print("[SYNC] Fetching TOC...", file=sys.stderr)
    toc = api_fetch(ctx, f"{BASE_URL}/d2l/api/le/1.57/{ou}/content/toc", token)
    if not toc:
        print("[SYNC] TOC fetch failed.", file=sys.stderr)
        return 1
    modules = toc.get("Modules", [])
    print(f"[SYNC] {len(modules)} top-level modules", file=sys.stderr)

    # ── 2. Assignments ───────────────────────────────────────────────────────
    print("[SYNC] Fetching assignments...", file=sys.stderr)
    folders_data = api_fetch(ctx,
                             f"{BASE_URL}/d2l/api/le/1.57/{ou}/dropbox/folders/", token)
    assignments = (folders_data.get("Folders", [])
                  if isinstance(folders_data, dict) else folders_data) or []
    print(f"[SYNC] {len(assignments)} assignment folders", file=sys.stderr)

    if assignments:
        assign_dir = out_dir / "_assignments"
        assign_dir.mkdir(parents=True, exist_ok=True)
        for folder in assignments:
            if not isinstance(folder, dict):
                continue
            fname = _safe(folder.get("Name", "untitled"))
            info = {
                "id": folder.get("Id"),
                "name": folder.get("Name"),
                "category_id": folder.get("CategoryId"),
                "start_date": folder.get("StartDate"),
                "due_date": folder.get("DueDate") or folder.get("EndDate"),
            }
            for key in ("CustomInstructions", "Instructions"):
                raw = folder.get(key, {})
                if isinstance(raw, dict):
                    info[f"{key}_text"] = raw.get("Text", "")
                    info[f"{key}_html"] = raw.get("Html", "")
                elif isinstance(raw, str):
                    info[key] = raw
            info_path = assign_dir / f"{fname}.json"
            try:
                info_path.write_text(json.dumps(info, indent=2, default=str))
            except Exception:
                pass
            html_content = (info.get("CustomInstructions_html", "")
                            or info.get("Instructions", ""))
            if html_content:
                for href in re.findall(r'href="(/content/enforced/[^"]+)"',
                                       html_content):
                    abs_url = BASE_URL + href
                    fname2 = _safe(Path(href.split("/")[-1]).stem or fname)
                    ext2 = _ext(abs_url)
                    dest2 = assign_dir / f"{fname2}{ext2 or '.file'}"
                    _download(abs_url, dest2, label=f"[{fname}] {dest2.name}")

    # ── 3. Google Sheets from TOC descriptions ──────────────────────────────
    print("[SYNC] Scanning for Google Sheets...", file=sys.stderr)
    sheets_found = 0
    for mod in modules:
        if not isinstance(mod, dict):
            continue
        for field in ("Title", "Description"):
            val = mod.get(field, "")
            if isinstance(val, dict):
                val = val.get("Html", val.get("Text", ""))
            csv_urls = extract_google_sheets_urls(str(val))
            for csv_url in csv_urls:
                sheets_found += 1
                safe = _safe(mod.get("Title", "schedule"))[:80]
                dest = out_dir / f"_schedule/{safe}.csv"
                download_google_sheet(csv_url, dest,
                                      label=f"Schedule: {mod.get('Title','')}")

    # ── 4. Walk TOC ─────────────────────────────────────────────────────────
    def walk_modules(mod_list: list, path: str = ""):
        nonlocal n_files
        for mod in mod_list:
            if not isinstance(mod, dict):
                continue
            title = _safe(mod.get("Title", ""))
            mp = f"{path}/{title}" if path else title
            mod_id = mod.get("ModuleId")
            mod_dir = out_dir / mp.replace("/", "_")
            mod_dir.mkdir(parents=True, exist_ok=True)

            for topic in mod.get("Topics", []):
                if not isinstance(topic, dict):
                    continue
                t_url   = topic.get("Url", "") or ""
                t_title = _safe(topic.get("Title", ""))
                t_id    = topic.get("TopicId", "") or ""
                t_type  = topic.get("Type", topic.get("TypeIdentifier", "")) or ""

                if not t_url:
                    continue
                if t_url.startswith("/"):
                    t_url = BASE_URL + t_url

                ext = _ext(t_url)
                is_file = (t_type in ("File", "Attachment") or
                           bool(ext) and t_type not in ("ExternalLink", "LTI"))

                if is_file:
                    dest = mod_dir / f"{t_title}{ext}"
                    _download(t_url, dest, label=t_title + ext)
                elif t_type == "ExternalLink":
                    (mod_dir / f"{t_title}.url").write_text(
                        f"[InternetShortcut]\nURL={t_url}\n")
                elif t_id and mod_id:
                    bd = api_fetch(ctx,
                        f"{BASE_URL}/d2l/api/le/1.57/{ou}/content/"
                        f"{mod_id}/topics/{t_id}/", token, timeout=15000)
                    if bd:
                        html = (bd.get("Body") or bd.get("Html")
                                or bd.get("Content") or "")
                        if html:
                            (mod_dir / f"{t_title}.html").write_text(html)
                            n_files += 1
                            print(f"  ✓ {t_title}.html", file=sys.stderr)
                        else:
                            (mod_dir / f"{t_title}.json").write_text(
                                json.dumps(bd, indent=2))
                            n_files += 1

                meta = {k: v for k, v in topic.items()
                        if k not in ("Modules", "Topics")}
                if meta:
                    try:
                        (mod_dir / f"{t_title}.meta.json").write_text(
                            json.dumps(meta, indent=2, default=str))
                    except Exception:
                        pass

            if mod.get("Modules"):
                walk_modules(mod["Modules"], mp)

    walk_modules(modules)

    # ── 5. Post-process HTML pages ─────────────────────────────────────────
    print("[SYNC] Scanning pages for linked files...", file=sys.stderr)
    base_urls: dict[str, str] = {}
    for meta_path in out_dir.rglob("*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text())
            topic_url = meta.get("Url") or ""
            if topic_url and topic_url.startswith("/content/enforced/"):
                parent_path = "/".join(topic_url.rstrip("/").split("/")[:-1])
                html_stem = (meta_path.parent.name + "/" +
                             meta_path.name.rsplit(".meta.json", 1)[0]
                             .rsplit(".html", 1)[0]).rstrip("/")
                base_urls[html_stem] = BASE_URL + parent_path + "/"
        except Exception:
            pass

    seen_urls: set[str] = set()
    for mod in modules:
        for t in mod.get("Topics", []):
            u = t.get("Url") or ""   # JSON may carry an explicit null Url
            if u.startswith("/"):
                seen_urls.add(BASE_URL + u)
            elif u:
                seen_urls.add(u)

    files_found = 0
    files_downloaded = 0

    def _resolve_and_download(html_path: Path, base_url: str, depth: int = 0):
        nonlocal files_found, files_downloaded, n_files, skipped
        if depth > 2 or not base_url:
            return
        try:
            text = html_path.read_text(errors="replace")
        except Exception:
            return

        hrefs = re.findall(r'href="([^"#]+)"', text)
        srcs  = re.findall(r'src="([^"#]+)"', text)
        for link in hrefs + srcs:
            if (link.startswith("javascript") or link.startswith("#") or
                    link.startswith("mailto:") or
                    "://" in link and not link.startswith(BASE_URL)):
                if not link.startswith("/"):
                    continue
            elif (link.startswith("/") and
                  not link.startswith("/content/") and
                  not link.startswith("/d2l/")):
                continue

            abs_url = (BASE_URL + link if link.startswith("/")
                       else str(urljoin(base_url, link)))

            if abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)

            ext = _ext(abs_url)
            if not ext:
                continue

            files_found += 1
            fname_raw = abs_url.split("/")[-1].split("?")[0]
            fname_base = (fname_raw.rsplit(".", 1)[0]
                          if "." in fname_raw else fname_raw)
            linked_dir = out_dir / "_linked_files" / html_path.stem
            dest = linked_dir / f"{_safe(fname_base)}{ext}"

            if _download(abs_url, dest,
                        label=f"[{html_path.stem[:30]}] {_safe(fname_base)}{ext}"):
                files_downloaded += 1
                if ext in (".html", ".htm") and depth < 2:
                    _resolve_and_download(dest,
                                          abs_url.rsplit("/", 1)[0] + "/",
                                          depth + 1)

    for html_path in out_dir.rglob("*.html"):
        if html_path.name.startswith("."):
            continue
        base = base_urls.get(f"{html_path.parent.name}/{html_path.stem}", "")
        _resolve_and_download(html_path, base, depth=0)

    print(f"[SYNC] Linked files: {files_found} found, "
          f"{files_downloaded} new downloads", file=sys.stderr)
    print(f"[SYNC] Google Sheets: {sheets_found} found", file=sys.stderr)
    print(f"\n[SYNC] Done: {n_files} downloaded, {skipped} skipped "
          f"→ {out_dir}", file=sys.stderr)

    # ── cleanup: close download page to free memory ──
    if dl_page is not None:
        try:
            dl_page.close()
        except Exception:
            pass

    return 0

# ── Token helper ───────────────────────────────────────────────────────────────
def _get_token(ctx: BrowserContext, args) -> Optional[str]:
    token = (args.token
             or extract_token(ctx)
             or (load_token() or {}).get("token"))
    if not token:
        print("No token. Run 'brightspace-cli.py login' first.", file=sys.stderr)
        return None
    return token

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Brightspace CLI (pure Playwright)")
    p.add_argument("--session-dir", default=str(SESSION_DIR))
    p.add_argument("--solisid", default=os.environ.get("BRIGHTSPACE_SOLISID"))
    p.add_argument("--password",  default=os.environ.get("BRIGHTSPACE_PASSWORD", ""))
    p.add_argument("--totp",      default=os.environ.get("BRIGHTSPACE_TOTP_SECRET", ""))
    p.add_argument("--token",    default=None, help="Bearer token (skips login if provided)")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login",       help="Authenticate and cache token")
    sub.add_parser("courses",     help="List enrolled courses")
    toc = sub.add_parser("toc",          help="Show course TOC")
    toc.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    ass = sub.add_parser("assignments",  help="List assignments (dropbox + LTI links)")
    ass.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    grd = sub.add_parser("grades",       help="Show grades")
    grd.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    nws = sub.add_parser("news",         help="Show announcements")
    nws.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    sch = sub.add_parser("schedule",     help="Download Google Sheets from course content")
    sch.add_argument("-o", "--output",   default=None)
    sch.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    ddl = sub.add_parser("deadlines",    help="Show all upcoming deadlines")
    ddl.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    syn = sub.add_parser("sync",         help="Sync all course content")
    syn.add_argument("--org-unit", default=os.environ.get("BRIGHTSPACE_ORG_UNIT"))
    syn.add_argument("-o", "--output",   default=None)

    args = p.parse_args()

    if args.cmd not in ("login", "courses") and not args.org_unit:
        p.error("--org-unit or BRIGHTSPACE_ORG_UNIT required for content commands")
    if args.cmd == "login" and not args.token and not args.solisid:
        p.error("--solisid or BRIGHTSPACE_SOLISID required for login")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(args.session_dir),
            headless=True,
            no_viewport=True,
            args=[
                "--no-sandbox",
                # ── Single-process & memory reduction ──
                "--single-process",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-dev-shm-usage",
                # ── Strip non-essential features ──
                "--disable-extensions",
                "--disable-sync",
                "--disable-default-apps",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-features=TranslateUI,BackForwardCache",
                "--no-first-run",
            ],
        )

        if args.token:
            save_token(args.token)
            print("Token provided and cached.", file=sys.stderr)

        elif args.cmd == "login":
            if not args.password or not args.totp:
                print("ERROR: --password and --totp required for login",
                      file=sys.stderr)
                sys.exit(1)
            do_login(ctx, args.solisid, args.password, args.totp)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"{BASE_URL}/d2l/home", wait_until="load", timeout=15000)
            page.wait_for_timeout(2000)
            token = extract_token(ctx)
            if token:
                save_token(token)
                print("Token cached.", file=sys.stderr)
            sys.exit(0)

        else:
            cached = load_token()
            if cached and cached.get("token"):
                print(f"[MAIN] Using cached token from {TOKEN_FILE}",
                      file=sys.stderr)
                args.token = cached["token"]
            else:
                if not args.password or not args.totp:
                    print("ERROR: No cached token and no --password/--totp.",
                          file=sys.stderr)
                    sys.exit(1)
                print("[MAIN] No token — logging in...", file=sys.stderr)
                do_login(ctx, args.solisid, args.password, args.totp)
                page = ctx.pages[0]
                # Do not re-navigate — do_login already lands on /d2l/home
                page.wait_for_timeout(2000)
                token = extract_token(ctx)
                if token:
                    save_token(token)
                args.token = token

        cmds = {
            "login":       cmd_login,
            "courses":     cmd_courses,
            "toc":         cmd_toc,
            "assignments": cmd_assignments,
            "grades":      cmd_grades,
            "news":        cmd_news,
            "schedule":    cmd_schedule,
            "deadlines":   cmd_deadlines,
            "sync":        cmd_sync,
        }
        sys.exit(cmds[args.cmd](ctx, args))

if __name__ == "__main__":
    main()
