"""Mirror course content to disk: sync (one course) and sync-all (every enrolled course)."""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import BrowserContext, Page

from uu_cli import auth, paths
from uu_cli.login import log_in
from uu_cli.paths import safe_filename
from uu_cli.services.brightspace.api import (
    BASE_URL, api_fetch, extract_token, get_token, load_token, save_token, token_expired,
)
from uu_cli.services.brightspace.discussions import save_discussions
from uu_cli.services.brightspace.sheets import download_google_sheet, extract_google_sheets_urls


def description_files(html: str) -> list[tuple[str, str]]:
    """(URL, filename) of each course file a description links to with a quickLink."""
    files = []
    for href in re.findall(r'href="([^"]*quickLink\.d2l[^"]*)"', html):
        url = urljoin(BASE_URL + "/", html_lib.unescape(href))
        query = parse_qs(urlparse(url).query)
        file_id = (query.get("fileId") or [""])[0]
        if query.get("type") != ["coursefile"] or not Path(file_id).name:
            continue
        files.append((url, safe_filename(Path(file_id).name)))
    return files


def course_dir(base_dir: Path, ou: str, name: Optional[str]) -> Path:
    """<base>/<course name> ('/' as '_'), or <base>/<ou> without a name.

    A folder from before sync named them (<base>/<ou>) is renamed to the course's name.
    """
    folder = safe_filename(name).replace("/", "_") if name else ""
    if folder in ("", ".", ".."):
        return base_dir / ou
    named, legacy = base_dir / folder, base_dir / ou
    if legacy.is_dir() and not named.exists():
        legacy.rename(named)
        print(f"[SYNC] Renamed {legacy.name}/ to {named.name}/", file=sys.stderr)
    return named


def course_name(ctx: BrowserContext, token: str, ou: str) -> Optional[str]:
    """The course's name in Brightspace (OrgUnit.Name of your enrollment), or None."""
    data = api_fetch(ctx, f"{BASE_URL}/d2l/api/lp/1.43/enrollments/myenrollments/{ou}", token)
    org = data.get("OrgUnit") if isinstance(data, dict) else None
    name = org.get("Name") if isinstance(org, dict) else None
    return name if isinstance(name, str) and name.strip() else None


def cmd_sync(ctx: BrowserContext, args) -> int:
    """Download ALL course content files to a local directory."""
    status, _, _ = _sync_course(ctx, args, args.org_unit, args.output)
    return status


def cmd_sync_all(ctx: BrowserContext, args) -> int:
    """Sync every enrolled course offering."""
    token = get_token(ctx, args)
    if not token:
        return 1
    data = api_fetch(ctx,
        f"{BASE_URL}/d2l/api/lp/1.43/enrollments/myenrollments/", token)
    items = data.get("Items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        print("[SYNC-ALL] Enrollment fetch failed or returned invalid data.", file=sys.stderr)
        return 1

    courses = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        org = item.get("OrgUnit")
        if not isinstance(org, dict):
            continue
        org_type = org.get("Type")
        if not isinstance(org_type, dict) or org_type.get("Id") != 3:
            continue
        if org.get("Id") is None:
            continue
        ou = str(org["Id"])
        if ou in seen:
            continue
        seen.add(ou)
        courses.append((ou, org))

    if args.dry_run:
        print(f"\n{'Code':<12} {'Name':<45} {'OrgUnitId'}")
        print("-" * 75)
        for ou, org in courses:
            print(f"{org.get('Code') or '':<12} {org.get('Name') or '':<45} {ou}")
        print(f"{len(courses)} course(s) would be synced.")
        return 0

    results = []
    failed = False
    for ou, org in courses:
        print(f"[SYNC-ALL] {org.get('Code') or ''} {org.get('Name') or ''} ({ou})",
              file=sys.stderr)
        try:
            status, downloaded, skipped = _sync_course(ctx, args, ou, args.output,
                                                       name=org.get("Name"))
            result = (f"{downloaded} downloaded, {skipped} skipped"
                      if status == 0 else "error: sync failed (see log)")
            failed = failed or status != 0
        except Exception as e:
            result = f"error: {e}"
            failed = True
        if result.startswith("error:"):
            print(f"[SYNC-ALL] {ou}: {result}", file=sys.stderr)
        results.append((ou, result))

    print("\nSync summary:")
    for ou, result in results:
        print(f"{ou} -> {result}")
    if not results:
        print("No matching enrolled courses.")
    return 1 if failed else 0


def _sync_course(ctx: BrowserContext, args, ou: str, output: Optional[str],
                 name: Optional[str] = None) -> tuple[int, int, int]:
    """Download ALL course content files to <output>/<course name>/ (name looked up when not given)."""
    # Fill gaps only: a password typed at the prompt route must survive the next course.
    args.password = args.password or os.environ.get("BRIGHTSPACE_PASSWORD", "")
    args.totp     = args.totp     or os.environ.get("BRIGHTSPACE_TOTP_SECRET", "")

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
        if token and token_expired(token):
            token = ""
        if token:
            print("[SYNC] Reusing authenticated browser session.", file=sys.stderr)

    if not token:
        if not auth.ready_to_login(args):
            print(f"ERROR: No authenticated session; {auth.CANNOT_LOGIN}.", file=sys.stderr)
            return 1, 0, 0
        print("[SYNC] Session expired — fresh login...", file=sys.stderr)
        log_in(ctx, BASE_URL, args)
        # Do not re-navigate — log_in already lands on /d2l/home
        page.wait_for_timeout(2000)

        fresh_token = extract_token(ctx)
        if fresh_token:
            save_token(fresh_token)
        token = fresh_token or (load_token() or {}).get("token", "")
        if not token:
            print("[SYNC] Token extraction failed.", file=sys.stderr)
            return 1, 0, 0
    print(f"[SYNC] Session ready.", file=sys.stderr)

    base_dir = Path(output or
                    str(paths.OUTPUT_DIR)).resolve()
    out_dir = course_dir(base_dir, ou, name or course_name(ctx, token, ou))
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
        return 1, 0, 0
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
            fname = safe_filename(folder.get("Name", "untitled"))
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
                    fname2 = safe_filename(Path(href.split("/")[-1]).stem or fname)
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
                safe = safe_filename(mod.get("Title", "schedule"))[:80]
                dest = out_dir / f"_schedule/{safe}.csv"
                download_google_sheet(csv_url, dest,
                                      label=f"Schedule: {mod.get('Title','')}")

    # ── 4. Walk TOC ─────────────────────────────────────────────────────────
    def walk_modules(mod_list: list, path: str = ""):
        nonlocal n_files
        for mod in mod_list:
            if not isinstance(mod, dict):
                continue
            title = safe_filename(mod.get("Title", ""))
            mp = f"{path}/{title}" if path else title
            mod_id = mod.get("ModuleId")
            mod_dir = out_dir / mp.replace("/", "_")
            mod_dir.mkdir(parents=True, exist_ok=True)

            # Some courses keep their slides as quickLinks in the module description, not as topics.
            desc = mod.get("Description")
            if isinstance(desc, dict) and desc.get("Html"):
                if desc.get("Text", "").strip():
                    (mod_dir / "_description.txt").write_text(desc["Text"])
                for url, name in description_files(desc["Html"]):
                    dest = mod_dir / name
                    _download(url, dest, label=dest.name)

            for topic in mod.get("Topics", []):
                if not isinstance(topic, dict):
                    continue
                t_url   = topic.get("Url", "") or ""
                t_title = safe_filename(topic.get("Title", ""))
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
            dest = linked_dir / f"{safe_filename(fname_base)}{ext}"

            if _download(abs_url, dest,
                        label=f"[{html_path.stem[:30]}] {safe_filename(fname_base)}{ext}"):
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

    # ── 6. Discussions ─────────────────────────────────────────────────────────
    print("[SYNC] Fetching discussions...", file=sys.stderr)
    try:
        n_posts = save_discussions(ctx, token, ou, out_dir)
        print(f"[SYNC] Discussions: {n_posts} posts", file=sys.stderr)
    except (Exception, SystemExit) as e:  # api_fetch exits on a 401; the files are already synced
        print(f"[SYNC] Discussions failed, skipped: {e}", file=sys.stderr)

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

    return 0, n_files, skipped
