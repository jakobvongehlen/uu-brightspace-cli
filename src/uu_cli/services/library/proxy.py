"""An article through UU's library proxy, in a browser window on your screen.

UU's library reaches publishers through an EZproxy at utrechtuniversity.idm.oclc.org: its
login link logs you in with your Solis-id and then opens the article as a UU subscriber. The
CLI builds that link and opens it in a window with your UU session. With `open`, finding the
PDF and opening or downloading it is up to you, as on any other day; with `get`, the window
opens the PDF the article page names in its citation_pdf_url tag (the one Google Scholar
reads), and you take over when it can't. Every PDF opened or downloaded there is saved, so one
window works for every publisher without code per publisher. A whole issue as a ZIP counts
when one PDF in it carries the article's DOI: that one is taken out. Any other download that
isn't a PDF is kept too, but doesn't count as the article. The window says on stderr which
pages it opens and what it downloads, so you can see where it got stuck.
"""

from __future__ import annotations

import datetime
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    BrowserContext, ConsoleMessage, Download, Error as PlaywrightError, Frame, Page, Request,
    Response,
)

from uu_cli import browser
from uu_cli.services.library import handoff
from uu_cli.services.library.articles import doi_of, save_file, save_pdf

PROXY = "https://utrechtuniversity.idm.oclc.org"

# How long the window stays open for you to find the PDF.
WINDOW_TIMEOUT_S = 900

# How long `get` waits for the PDF it opened before it asks you to open it.
FETCH_WAIT_S = 30

# The requests the window reports when they fail or are refused: pages, and what their scripts
# fetch (a "View PDF" button that asks for its link first). Images, fonts and the like are left out.
REPORTED = ("document", "xhr", "fetch")

# Refusals that mean a site turned the window or the proxy away, and what to do instead. On
# 25-10-2026 ScienceDirect's "View PDF" went through Cloudflare's check (…/cdn-cgi/…) and got a
# 403, and IEEE Xplore answered its article pages with 418, in your own browser too.
CLOUDFLARE = ("Cloudflare's bot check turned this window away. On ScienceDirect, \"Download full "
              "issue\" works: the window takes the article out of the ZIP. Or start with --pickup "
              "<folder>, and the window hands the article to your own browser.")
TEAPOT = ("The site turned UU's library proxy away (418), as IEEE Xplore does in any browser: "
          "try the publisher's own site with its institutional sign-in.")

# The refusals after which --pickup hands the article to your own browser (handoff.py).
TURNED_AWAY = {CLOUDFLARE, TEAPOT}

# The largest file in a ZIP the window looks into for the article; an issue's PDFs are a few MB.
ZIP_MEMBER_MAX = 100_000_000

# Every click in the window, what the site sent back in the seconds after it, and the PDF that
# came of it, one JSON object per line in the output folder: to see per publisher which button
# gives the PDF, so the window can learn to press it itself (28-10-2026). Addresses go in
# without their query values, which can hold a login ticket or a signed download link.
CLICK_LOG = "clicks.jsonl"

# How long after a click what the site sends back is written down with it.
AFTER_CLICK_S = 10

# What the log keeps of what comes back after a click: pages and script fetches through the
# proxy, and files from anywhere. The rest is analytics: on 29-10-2026, 381 of 408 lines were
# New Relic, Adobe, Pendo and Facebook.
PROXY_HOST = urlparse(PROXY).hostname
FILE_TYPES = ("application/pdf", "application/zip", "application/octet-stream")

# Reports each click to the log: the link or button around the spot clicked, and whether you made
# it (a page's own script can click too). Runs in every page and frame of the window. A click in a
# form field is left out, and a field's value is never read: it can be the password you typed.
CLICK_SPY = """(() => {
  const BUTTONS = 'a, button, [role="button"], input[type="submit"], input[type="button"]'
  document.addEventListener('click', (event) => {
    if (!window.__uuClick || !(event.target instanceof Element)) return
    const el = event.target.closest(BUTTONS) || event.target
    if (!el.matches(BUTTONS) && el.closest('input, textarea, select, [contenteditable]')) return
    const label = el.matches('input') ? el.value : el.innerText
    window.__uuClick({
      tag: el.tagName.toLowerCase(), id: el.id || '', classes: String(el.getAttribute('class') || ''),
      text: (label || el.getAttribute('aria-label') || el.title || '').trim(),
      href: typeof el.href === 'string' ? el.href : '', by_you: event.isTrusted,
    })
  }, true)
})()"""

# The PDF an article page names for search engines, as an absolute address.
CITATION_PDF = """() => {
  const meta = document.querySelector('meta[name="citation_pdf_url"]')
  return meta && meta.content ? new URL(meta.content, location.href).href : null
}"""


def proxy_url(article: str) -> str:
    """The proxy's login link for article; auth=uushibboleth is its "Universiteit Utrecht" choice
    (the other one is UMC Utrecht), so the proxy goes straight to UU's login."""
    return f"{PROXY}/login?auth=uushibboleth&url={article}"


def open_article(ctx: BrowserContext, article: str, out_dir: Path, doi: str | None,
                 keep_open: bool = False, fetch: bool = False,
                 pickup: Path | None = None) -> tuple[list[Path], list[Path]]:
    """Open article through the proxy in a window with ctx's session; returns the PDFs saved,
    and the other files downloaded there.

    fetch: once the article page is there, open the PDF it names in a tab of the window (`get`).
    pickup: when the site turns the window away (TURNED_AWAY), close it and hand the article to
    your own browser; the PDF you save into pickup is taken from there (handoff.py).
    A PDF is named after its own DOI when its address has one, else after the article's doi:
    a chapter page can also offer the whole book. A download that isn't a PDF keeps its own
    name and leaves the window open. The window closes once a PDF is saved (unless keep_open),
    when you close it, or after WINDOW_TIMEOUT_S. Its session goes back into ctx, so the next
    run skips the logins.
    """
    window = browser.open_window(ctx)
    found: list[Download | Response] = []  # saved from the loop below, not from the handlers
    said: set[str] = set()  # what the window reported going wrong, each said once
    clicks = ClickLog(out_dir / CLICK_LOG, doi)
    window.on("response", clicks.response)

    def watch(page: Page) -> None:
        if len(window.pages) > 1:
            print("[LIBRARY] New tab", file=sys.stderr)
        page.on("download", lambda d: found.append(d))
        page.on("framenavigated", lambda frame: _say_where(frame) if not frame.parent_frame else None)
        page.on("requestfailed", lambda request: _note_failure(request, said))
        page.on("response", lambda response: _note_refusal(response, said))
        page.on("console", lambda message: _note_console(message, said))

    window.on("page", watch)
    window.on("response", lambda r: found.append(r) if _is_pdf(r) else None)
    saved: list[Path] = []
    others: list[Path] = []  # downloads that aren't a PDF
    fetched_at = 0.0  # when `get` opened the PDF; 0 while it hasn't
    handing_off = False
    try:
        window.expose_binding("__uuClick", clicks.click)
        window.add_init_script(CLICK_SPY)
        _first_page(window.new_page(), proxy_url(article), found)
        deadline = time.monotonic() + WINDOW_TIMEOUT_S
        while time.monotonic() < deadline:
            while found:
                item = found.pop(0)
                own = doi_of(item.url)
                path = _save(item, window, out_dir, own or doi or "article", others, doi)
                if path and path not in saved:
                    saved.append(path)
                    clicks.saved(path, item)
                    print(f"Saved: {path}")
                    if own and doi and own != doi:
                        print(f"[LIBRARY] That PDF has its own DOI, {own}, not the article's: "
                              "probably the whole book or issue.", file=sys.stderr)
            if saved and not keep_open:
                break
            if pickup and doi and not saved and said & TURNED_AWAY:
                handing_off = True
                break
            pages = [p for p in window.pages if not p.is_closed()]
            if not pages:
                break
            if fetch and not fetched_at and (pdf := _citation_pdf(pages)):
                print(f"[LIBRARY] Opening the article's PDF: {pdf.split('?')[0]}", file=sys.stderr)
                _open_pdf(window, pdf)
                fetched_at = time.monotonic()
            elif fetch and fetched_at and not saved and time.monotonic() > fetched_at + FETCH_WAIT_S:
                print("[LIBRARY] The PDF didn't come by itself: open or download it in the window.",
                      file=sys.stderr)
                fetch = False
            try:
                pages[0].wait_for_timeout(250)
            except PlaywrightError:
                pass  # closed meanwhile; the next round sees it
        else:
            print(f"[LIBRARY] Closing the window after {WINDOW_TIMEOUT_S // 60} minutes.",
                  file=sys.stderr)
        try:
            ctx.add_cookies(window.cookies())
        except PlaywrightError:
            pass  # you closed the browser itself; the next run just logs in again
    finally:
        try:
            window.browser.close()
        except Exception:  # Ctrl+C stops Playwright's driver too: then there's nothing to close
            pass
    if handing_off and (path := handoff.hand_off(proxy_url(article), doi, out_dir, pickup)):
        saved.append(path)
        print(f"Saved: {path}")
    return saved, others


def _first_page(page: Page, url: str, found: list) -> None:
    """Open the proxy's link for the article, once more if the first try stops before the page.

    A link that turns into a download stops too (net::ERR_ABORTED); the download then arrives in
    found, and the loop saves it. On 25-10-2026 one such stop ended a `watch` run halfway.
    """
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until="commit", timeout=60_000)
            return
        except PlaywrightError as err:
            reason = _reason(err)
        try:
            page.wait_for_timeout(1000)  # a download that stopped it arrives meanwhile
        except PlaywrightError:
            return  # the window is gone; the loop sees it
        if found:
            return
        then = "trying once more" if attempt == 1 else "open the article in the window, or close it"
        print(f"[LIBRARY] The article's page didn't open ({reason}): {then}.", file=sys.stderr)


def _reason(err: PlaywrightError) -> str:
    """Chromium's reason in a navigation error (net::ERR_…), not its address."""
    found = re.search(r"net::ERR_\w+", str(err))
    return found.group(0) if found else str(err).splitlines()[0][:100]


def _say_where(frame: Frame) -> None:
    print(f"[LIBRARY] Page: {_where(frame.url)}", file=sys.stderr)


def _say_once(said: set[str], line: str) -> None:
    if line not in said:
        said.add(line)
        print(f"[LIBRARY] {line}", file=sys.stderr)


def _note_failure(request: Request, said: set[str]) -> None:
    """A page, or a request a page's script made, that failed: blocked, refused or unreachable.
    An aborted one is left out: a link that turned into a download, or the window closing."""
    try:
        kind, failure = request.resource_type, request.failure
    except PlaywrightError:
        return
    if kind in REPORTED and failure != "net::ERR_ABORTED":
        _say_once(said, f"Failed: {_where(request.url)}: {failure}")


def _note_refusal(response: Response, said: set[str]) -> None:
    """A page, or what a page's script fetched, that came back refused or missing (4xx, 5xx)."""
    try:
        status, kind = response.status, response.request.resource_type
    except PlaywrightError:
        return
    if status >= 400 and kind in REPORTED:
        _say_once(said, f"{status} from {_where(response.url)}")
        if status == 403 and "/cdn-cgi/" in urlparse(response.url).path:
            _say_once(said, CLOUDFLARE)
        elif status == 418:
            _say_once(said, TEAPOT)


def _note_console(message: ConsoleMessage, said: set[str]) -> None:
    """A request the browser blocked (CORS, a content security policy, a pop-up), with its
    address without the query."""
    text = message.text
    if message.type == "error" and ("CORS policy" in text or "Refused to" in text or "Blocked" in text):
        line = re.sub(r"https?://[^\s'\"]+", lambda m: _where(m.group(0)), text.splitlines()[0])
        _say_once(said, f"The browser blocked a request: {line[:300]}")


def _where(url: str) -> str:
    """url without its query, which can hold a login ticket or a signed download link."""
    parts = urlparse(url)
    return f"{parts.netloc}{parts.path}" if parts.netloc else url


class ClickLog:
    """The clicks in the window, what the site sent back in the AFTER_CLICK_S after each, and the
    PDF saved, appended to path. The file is only made once there is a click to write down."""

    def __init__(self, path: Path, doi: str | None):
        self.path, self.doi = path, doi
        self.clicked_at = 0.0  # 0 while there was no click

    def click(self, source: dict, info: dict) -> None:
        """Called by CLICK_SPY in the page. What it sends is the page's own text: only the
        expected fields are kept, as short printable strings."""
        page = source.get("page")
        fields = {k: _printable(info.get(k), 120) for k in ("tag", "id", "classes", "text")}
        fields["href"] = _without_query_values(str(info.get("href") or ""))
        fields["by_you"] = info.get("by_you") is True
        self.clicked_at = time.monotonic()
        where = _where(page.url) if page else ""
        self._write({"page": where, "click": fields})
        label = fields["text"][:60] or fields["tag"]
        print(f"[LIBRARY] Click: {label!r} on {where}", file=sys.stderr)

    def response(self, response: Response) -> None:
        if not self.clicked_at or time.monotonic() > self.clicked_at + AFTER_CLICK_S:
            return
        try:
            kind, status = response.request.resource_type, response.status
            method = response.request.method
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
        except PlaywrightError:
            return
        host = urlparse(response.url).hostname or ""
        through_proxy = host == PROXY_HOST or host.endswith(f".{PROXY_HOST}")
        if (kind in REPORTED and through_proxy) or content_type in FILE_TYPES:
            self._write({"response": {"method": method, "kind": kind, "status": status,
                                      "type": content_type, "url": _without_query_values(response.url)}})

    def saved(self, path: Path, item: Download | Response) -> None:
        """The PDF a click led to; one that came without a click (`get`) is in watch.log already."""
        if not self.clicked_at:
            return
        self._write({"saved": path.name, "from": _without_query_values(item.url),
                     "as": "download" if isinstance(item, Download) else "page"})

    def _write(self, entry: dict) -> None:
        entry = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "doi": self.doi, **entry}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as err:
            print(f"[LIBRARY] Couldn't write to {self.path}: {err}", file=sys.stderr)


def _printable(value, limit: int) -> str:
    """value as a string a terminal can show: printable characters only, at most limit."""
    return "".join(c if c.isprintable() else " " for c in str(value or ""))[:limit]


def _without_query_values(url: str) -> str:
    """url as _where gives it, with the names of its query parameters but not their values:
    a signed download link's values are secrets, its names say how the site builds the link."""
    names = sorted({part.split("=", 1)[0] for part in urlparse(url).query.split("&") if part})
    return _where(url) + (f"?{'&'.join(names)}" if names else "")


def _citation_pdf(pages: list[Page]) -> str | None:
    """The PDF address an open page names in its citation_pdf_url tag, if one does."""
    for page in pages:
        try:
            pdf = page.evaluate(CITATION_PDF)
        except PlaywrightError:
            continue  # on its way to the next page
        if pdf:
            return pdf
    return None


def _open_pdf(window: BrowserContext, pdf: str) -> None:
    """Open pdf in a new tab, through the proxy when the page named the publisher's own address."""
    host = urlparse(pdf).hostname or ""
    proxy = urlparse(PROXY).hostname
    if host != proxy and not host.endswith(f".{proxy}"):
        pdf = proxy_url(pdf)
    try:
        window.new_page().goto(pdf, wait_until="commit", timeout=60_000)
    except PlaywrightError:
        pass  # a download ends the navigation; the loop saves it, or asks you to take over


def _is_pdf(response: Response) -> bool:
    """A PDF from a site; not the copy Chromium's PDF viewer passes on inside itself
    (chrome-extension://…), which can't be fetched again."""
    content_type = response.headers.get("content-type", "")
    return (response.url.startswith(("http://", "https://")) and response.status == 200
            and content_type.split(";")[0].strip() == "application/pdf")


def _save(item: Download | Response, window: BrowserContext, out_dir: Path, name: str,
          others: list[Path], doi: str | None = None) -> Path | None:
    """Save a download or a PDF that opened in a tab; None when it turns out not to be a PDF.

    A ZIP with exactly one PDF that carries doi gives that PDF. Any other download that isn't a
    PDF is kept under its own name and added to others.
    """
    if isinstance(item, Download):
        print(f"[LIBRARY] Download: {item.suggested_filename}", file=sys.stderr)
    try:
        if isinstance(item, Download):
            data = Path(item.path()).read_bytes()
        else:
            try:
                data = item.body()
            except PlaywrightError:  # shown in Chromium's PDF viewer: fetch it once more
                data = window.request.get(item.url).body()
    except (PlaywrightError, OSError, TypeError) as err:
        print(f"[LIBRARY] Couldn't read {_where(item.url)}: {err}", file=sys.stderr)
        return None
    if data.startswith(b"%PDF"):
        return save_pdf(out_dir, data, name)
    if not isinstance(item, Download):
        print(f"[LIBRARY] Not the PDF yet: {_where(item.url)} sent {_kind(data)} labelled as a "
              "PDF. Waiting for the PDF itself.", file=sys.stderr)
        return None
    if doi and data.startswith(b"PK"):
        hits, files = _article_in_zip(data, doi)
        if len(hits) == 1:
            print(f"[LIBRARY] Took {doi} out of the ZIP ({files} files).", file=sys.stderr)
            return save_pdf(out_dir, hits[0], name)
        if hits:
            print(f"[LIBRARY] {len(hits)} PDFs in the ZIP carry {doi}, so none is taken.",
                  file=sys.stderr)
    own = Path(item.suggested_filename)
    path = save_file(out_dir, data, own.stem, own.suffix)
    if path not in others:
        others.append(path)
        print(f"[LIBRARY] Kept {path}: it's {_kind(data)}, not a PDF, so it isn't the article. "
              "Open the article's own PDF in the window, or close it.", file=sys.stderr)
    return None


def _article_in_zip(data: bytes, doi: str) -> tuple[list[bytes], int]:
    """The PDFs in a ZIP that carry doi, and how many files it has. ScienceDirect's issue PDFs
    carry their own DOI in their metadata, which isn't compressed: in the issue that EMIL
    (10.1016/j.ijar.2019.04.010) came in, its PDF had it 11 times and the other 9 had only
    their own. The archive's names aren't used: they're cut-off titles."""
    needle = doi.lower().encode()
    hits: list[bytes] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = [info for info in archive.infolist() if not info.is_dir()]
            for info in files:
                if info.file_size > ZIP_MEMBER_MAX:
                    continue
                member = archive.read(info)
                if member.startswith(b"%PDF") and needle in member.lower():
                    hits.append(member)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, ValueError, EOFError, OSError):
        return [], 0  # not a ZIP after all, or one Python can't open: kept as it is
    return hits, len(files)


def _kind(data: bytes) -> str:
    """What a file that isn't a PDF looks like, for the message about it."""
    size = f"{len(data) / 1e6:.1f} MB" if len(data) >= 100_000 else f"{len(data)} bytes"
    if data.startswith(b"PK"):
        return f"a ZIP archive ({size})"
    if data.lstrip()[:1] == b"<":
        return f"a web page ({size})"
    return f"a file of {size}"
