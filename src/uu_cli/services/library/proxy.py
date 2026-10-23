"""An article through UU's library proxy, in a browser window on your screen.

UU's library reaches publishers through an EZproxy at utrechtuniversity.idm.oclc.org: its
login link logs you in with your Solis-id and then opens the article as a UU subscriber. The
CLI builds that link and opens it in a window with your UU session. With `open`, finding the
PDF and opening or downloading it is up to you, as on any other day; with `get`, the window
opens the PDF the article page names in its citation_pdf_url tag (the one Google Scholar
reads), and you take over when it can't. Every PDF opened or downloaded there is saved, so one
window works for every publisher without code per publisher. A download that isn't a PDF (a
whole issue as a ZIP) is kept too, but doesn't count as the article. The window says on stderr
which pages it opens and what it downloads, so you can see where it got stuck.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    BrowserContext, Download, Error as PlaywrightError, Frame, Page, Response,
)

from uu_cli import browser
from uu_cli.services.library.articles import doi_of, save_file, save_pdf

PROXY = "https://utrechtuniversity.idm.oclc.org"

# How long the window stays open for you to find the PDF.
WINDOW_TIMEOUT_S = 900

# How long `get` waits for the PDF it opened before it asks you to open it.
FETCH_WAIT_S = 30

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
                 keep_open: bool = False,
                 fetch: bool = False) -> tuple[list[Path], list[Path]]:
    """Open article through the proxy in a window with ctx's session; returns the PDFs saved,
    and the other files downloaded there.

    fetch: once the article page is there, open the PDF it names in a tab of the window (`get`).
    A PDF is named after its own DOI when its address has one, else after the article's doi:
    a chapter page can also offer the whole book. A download that isn't a PDF keeps its own
    name and leaves the window open. The window closes once a PDF is saved (unless keep_open),
    when you close it, or after WINDOW_TIMEOUT_S. Its session goes back into ctx, so the next
    run skips the logins.
    """
    window = browser.open_window(ctx)
    found: list[Download | Response] = []  # saved from the loop below, not from the handlers

    def watch(page: Page) -> None:
        page.on("download", lambda d: found.append(d))
        page.on("framenavigated", lambda frame: _say_where(frame) if not frame.parent_frame else None)

    window.on("page", watch)
    window.on("response", lambda r: found.append(r) if _is_pdf(r) else None)
    saved: list[Path] = []
    others: list[Path] = []  # downloads that aren't a PDF
    fetched_at = 0.0  # when `get` opened the PDF; 0 while it hasn't
    try:
        page = window.new_page()
        page.goto(proxy_url(article), wait_until="commit", timeout=60_000)
        deadline = time.monotonic() + WINDOW_TIMEOUT_S
        while time.monotonic() < deadline:
            while found:
                item = found.pop(0)
                own = doi_of(item.url)
                path = _save(item, window, out_dir, own or doi or "article", others)
                if path and path not in saved:
                    saved.append(path)
                    print(f"Saved: {path}")
                    if own and doi and own != doi:
                        print(f"[LIBRARY] That PDF has its own DOI, {own}, not the article's: "
                              "probably the whole book or issue.", file=sys.stderr)
            if saved and not keep_open:
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
    return saved, others


def _say_where(frame: Frame) -> None:
    print(f"[LIBRARY] Page: {_where(frame.url)}", file=sys.stderr)


def _where(url: str) -> str:
    """url without its query, which can hold a login ticket or a signed download link."""
    parts = urlparse(url)
    return f"{parts.netloc}{parts.path}" if parts.netloc else url


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
    content_type = response.headers.get("content-type", "")
    return response.status == 200 and content_type.split(";")[0].strip() == "application/pdf"


def _save(item: Download | Response, window: BrowserContext, out_dir: Path, name: str,
          others: list[Path]) -> Path | None:
    """Save a download or a PDF that opened in a tab; None when it turns out not to be a PDF.

    A download that isn't a PDF is kept under its own name and added to others.
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
        print(f"[LIBRARY] Skipped {_where(item.url)}: sent as a PDF, but it's {_kind(data)}.",
              file=sys.stderr)
        return None
    own = Path(item.suggested_filename)
    path = save_file(out_dir, data, own.stem, own.suffix)
    if path not in others:
        others.append(path)
        print(f"[LIBRARY] Kept {path}: it's {_kind(data)}, not a PDF, so it isn't the article. "
              "Open the article's own PDF in the window, or close it.", file=sys.stderr)
    return None


def _kind(data: bytes) -> str:
    """What a file that isn't a PDF looks like, for the message about it."""
    size = f"{len(data) / 1e6:.1f} MB" if len(data) >= 100_000 else f"{len(data)} bytes"
    if data.startswith(b"PK"):
        return f"a ZIP archive ({size})"
    if data.lstrip()[:1] == b"<":
        return f"a web page ({size})"
    return f"a file of {size}"
