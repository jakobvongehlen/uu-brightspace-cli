"""A Scopus search through UU's library, in a browser window on your screen.

Scopus is Elsevier's citation database, which UU subscribes to. Its terms forbid scripts that
harvest it, and its API needs a key plus UU's network or an institutional token. So the CLI
opens Scopus through UU's library proxy (the login `uu library open` uses), with the query in
the address of the results page, and you look at the hits and click Export there, as on any
other day. Every file you download in that window (CSV, RIS, BibTeX) is saved as
scopus-<date>.<ext> (-2, -3 and up for the next ones), and scopus.log in the same folder gets a
line per file: the date, the query, the file. The query is printed as well, to paste into the
advanced search if Scopus doesn't take it from the address.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from playwright.sync_api import (APIResponse, BrowserContext, ConsoleMessage, Download, Error as PlaywrightError, Page,
                                 Request, Response, Route)

from uu_cli import browser
from uu_cli.services.library import proxy
from uu_cli.services.library.articles import save_file

RESULTS = "https://www.scopus.com/results/results.uri"
LOG = "scopus.log"
# Content types an export or a full text comes as; any other response is a page, a script or data.
FILE_TYPES = ("csv", "research-info-systems", "bibtex", "octet-stream", "ms-excel", "spreadsheet", "zip", "pdf")
PAGE_PARTS = ("font", "image", "stylesheet", "script", "media")  # Scopus's fonts come as binary/octet-stream
EXPORT_FILE = re.compile(r"\.(csv|ris|bib|txt)$", re.IGNORECASE)
EXPORTS_HOSTS = ("exports-scopus-com.", "exports.scopus.com")  # behind the library proxy, and Scopus's own

# How long the window stays open: a search with a few exports takes longer than one article.
WINDOW_TIMEOUT_S = 1800

# Turns the browser's save dialog (showSaveFilePicker) into an ordinary download. A page that
# saves through the dialog writes the file itself, so the browser reports no download and the
# window never sees the file (measured 25-10-2026: an attachment, a blob: link, a fetch saved as
# a blob, a form post and a service worker's download all arrive; the dialog alone doesn't).
# With this script the page writes into memory, and closing the file starts a download of what
# it wrote, under the name the page suggested.
SAVE_DIALOG = """(() => {
  if (!("showSaveFilePicker" in window)) return;
  window.showSaveFilePicker = async (options = {}) => {
    const name = options.suggestedName || "download";
    console.log("uu-cli: save dialog for " + name + ", saved as a download instead");
    const parts = [];
    const keep = (data) => { parts.push(data && data.type === "write" ? data.data : data); };
    const save = () => {
      const a = document.createElement("a");
      a.href = window.URL.createObjectURL(new Blob(parts));
      a.download = name;
      document.body.append(a);
      a.click();
      a.remove();
    };
    const writable = () => {
      const stream = new WritableStream({ write: keep, close: save });
      stream.write = async (data) => keep(data);
      stream.close = async () => save();
      stream.seek = async () => {};
      stream.truncate = async () => {};
      return stream;
    };
    return { kind: "file", name, createWritable: async () => writable(),
             getFile: async () => new File(parts, name),
             queryPermission: async () => "granted", requestPermission: async () => "granted" };
  };
})();"""


def results_url(query: str) -> str:
    """Scopus's results page for an advanced-search query (sot=a: an advanced search)."""
    return f"{RESULTS}?sort=plf-f&src=s&sot=a&sdt=a&origin=searchadvanced&s={quote(query, safe='')}"


def search(ctx: BrowserContext, query: str, out_dir: Path) -> list[Path]:
    """Open Scopus with query through the proxy in a window with ctx's session; returns the files
    saved. The window closes when you close it, or after WINDOW_TIMEOUT_S; its session goes
    back into ctx, so the next query skips the logins."""
    window = browser.open_window(ctx)
    downloads: list[Download] = []  # saved from the loop below, not from the handler
    unfetched: list[str] = []  # exports the page asked for and didn't get; opened from the loop

    def watch(page: Page) -> None:
        page.on("download", lambda d: downloads.append(d))
        page.on("framenavigated", lambda frame: proxy._say_where(frame) if not frame.parent_frame else None)
        page.on("response", _note_file)
        page.on("requestfailed", lambda request: _note_failure(request, unfetched))
        page.on("console", _note_console)

    window.on("page", watch)
    window.add_init_script(SAVE_DIALOG)
    window.route(_on_exports_host, _allow_the_page)
    stamp = f"{dt.date.today():%Y-%m-%d}"
    saved: list[Path] = []
    opened: set[str] = set()

    def keep(path: Path | None) -> None:
        if path and path not in saved:
            saved.append(path)
            print(f"Saved: {path}")
            _log(out_dir, stamp, query, path)

    try:
        window.new_page().goto(proxy.proxy_url(results_url(query)), wait_until="commit", timeout=60_000)
        deadline = time.monotonic() + WINDOW_TIMEOUT_S
        while time.monotonic() < deadline:
            while downloads:
                keep(_save(downloads.pop(0), out_dir, stamp))
            while unfetched:
                url = unfetched.pop(0)
                if url not in opened:
                    opened.add(url)
                    keep(_open_in_tab(window, url, out_dir, stamp))
            pages = [p for p in window.pages if not p.is_closed()]
            if not pages:
                break
            try:
                pages[0].wait_for_timeout(250)
            except PlaywrightError:
                pass  # closed meanwhile; the next round sees it
        else:
            print(f"[SCOPUS] Closing the window after {WINDOW_TIMEOUT_S // 60} minutes.", file=sys.stderr)
        try:
            ctx.add_cookies(window.cookies())
        except PlaywrightError:
            pass  # you closed the browser itself; the next run just logs in again
    finally:
        try:
            window.browser.close()
        except Exception:  # Ctrl+C stops Playwright's driver too: then there's nothing to close
            pass
    return saved


def _on_exports_host(url: str) -> bool:
    return (urlparse(url).hostname or "").startswith(EXPORTS_HOSTS)


def _fetch_export(route: Route) -> APIResponse:
    return route.fetch()  # the tests stand in for this: their fake isn't on the network


def _allow_the_page(route: Route) -> None:
    """Hand the page its export with its own address allowed.

    Scopus prepares an export, then its page fetches the file from its exports host (exports.scopus.com). Through
    the library proxy the browser blocked that fetch for CORS (measured 25-10-2026, 9 exports of 9): the host's
    answer doesn't allow the proxy's address for Scopus's page. Here the window fetches the file itself, with the
    session's cookies, and passes it on with the page's address allowed: the page then saves it as usual.
    """
    try:
        response = _fetch_export(route)
    except PlaywrightError as err:
        print(f"[SCOPUS] Couldn't fetch the export: {str(err).splitlines()[0]}", file=sys.stderr)
        return route.fallback()  # the page's fetch fails, and _open_in_tab tries once more
    headers = dict(response.headers)
    origin = route.request.headers.get("origin")
    if origin:
        headers["access-control-allow-origin"] = origin
        headers["access-control-allow-credentials"] = "true"
    route.fulfill(status=response.status, headers=headers, body=response.body())


def _note_file(response: Response) -> None:
    """Say so when a file comes in (an attachment, or a type an export comes as), by its address without the query,
    which can hold a signed link: if an export arrives but no download follows, this shows where it went."""
    try:
        headers = response.headers
        if response.request.resource_type in PAGE_PARTS:
            return
    except PlaywrightError:
        return
    kind = headers.get("content-type", "").split(";")[0].strip().lower()
    attached = headers.get("content-disposition", "").lower().startswith("attachment")
    if attached or any(t in kind for t in FILE_TYPES):
        print(f"[SCOPUS] File: {response.status} {kind or 'no type'}{', attachment' if attached else ''} "
              f"from {proxy._where(response.url)}", file=sys.stderr)


def _note_failure(request: Request, unfetched: list[str]) -> None:
    """An export request that failed. The export file itself goes on the list to open in a tab. An aborted one is
    left alone and unsaid: a link that turned into a download (which arrives anyway), or the window closing."""
    if "export" not in request.url.lower() or request.failure == "net::ERR_ABORTED":
        return
    print(f"[SCOPUS] Failed: {proxy._where(request.url)}: {request.failure}", file=sys.stderr)
    if EXPORT_FILE.search(urlparse(request.url).path):
        unfetched.append(request.url)


def _note_console(message: ConsoleMessage) -> None:
    """The save dialog script's notes, and a request the browser blocked for CORS."""
    text = message.text
    if text.startswith("uu-cli: "):
        print(f"[SCOPUS] {text.removeprefix('uu-cli: ')}", file=sys.stderr)
    elif "CORS policy" in text:
        url = re.search(r"https?://[^\s'\"]+", text)
        print(f"[SCOPUS] The browser blocked {proxy._where(url.group(0)) if url else 'a request'} (CORS)",
              file=sys.stderr)


def _open_in_tab(window: BrowserContext, url: str, out_dir: Path, stamp: str) -> Path | None:
    """Open an export the page couldn't fetch in a tab of its own, and save it: the way back when _allow_the_page
    couldn't fetch it. Opening the file as a page isn't subject to CORS: it comes as a download, which the loop saves,
    or as the page itself. It's the one file you asked for (measured 25-10-2026: 9 exports of 9)."""
    print("[SCOPUS] The page couldn't fetch its export; opening it in a tab of its own", file=sys.stderr)
    tab = window.new_page()
    try:
        response = tab.goto(url, timeout=60_000)
    except PlaywrightError as err:
        if "Download is starting" not in str(err):
            print(f"[SCOPUS] Couldn't open it: {str(err).splitlines()[0]}", file=sys.stderr)
        return None  # a download: the loop saves it
    if response is None or not response.ok:
        print(f"[SCOPUS] Couldn't open it: {response.status if response else 'no answer'}", file=sys.stderr)
        return None
    data = response.body()
    tab.close()
    return save_file(out_dir, data, f"scopus-{stamp}", Path(urlparse(url).path).suffix or ".csv")


def _save(download: Download, out_dir: Path, stamp: str) -> Path | None:
    print(f"[SCOPUS] Download: {download.suggested_filename}", file=sys.stderr)
    try:
        data = Path(download.path()).read_bytes()
    except (PlaywrightError, OSError, TypeError) as err:
        print(f"[SCOPUS] Couldn't read the download: {err}", file=sys.stderr)
        return None
    return save_file(out_dir, data, f"scopus-{stamp}", Path(download.suggested_filename).suffix)


def _log(out_dir: Path, stamp: str, query: str, path: Path) -> None:
    one_line = re.sub(r"\s+", " ", query).strip()
    with (out_dir / LOG).open("a", encoding="utf-8") as f:
        f.write(f"{stamp}\t{one_line}\t{path.name}\n")
