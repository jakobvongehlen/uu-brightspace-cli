"""`uu library <command>`: articles from journals UU subscribes to."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from uu_cli import auth, browser, paths
from uu_cli.services.library import articles, elsevier, proxy

# Between two articles from Elsevier's API: one researcher's pace, not a crawler's.
PAUSE_S = 1.0

# `watch`: the list it reads and the log it writes (in the output folder), how often it looks
# at the list, the pause between two windows, and the most articles one run fetches, so a list
# that keeps growing can't turn into systematic downloading.
WANTED = "wanted.txt"
WATCH_LOG = "watch.log"
WATCH_POLL_S = 2.0
WATCH_PAUSE_S = 10.0
WATCH_MAX = 25


def cmd_open(args) -> int:
    """Open an article through UU's library in a window; you open or download the PDF there."""
    return _one_article(args, fetch=False)


def cmd_get(args) -> int:
    """Open an article through UU's library in a window that opens the PDF itself."""
    return _one_article(args, fetch=True)


def _one_article(args, fetch: bool) -> int:
    if not _has_screen(args.cmd):
        return 1
    try:
        url = articles.article_url(args.article)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    out = Path(args.output)
    then = "the PDF opens by itself" if fetch else "open or download the PDF"
    print(f"[LIBRARY] Opening the article through UU's library. Log in there if UU asks, then "
          f"{then}; it's saved to {out}/.", file=sys.stderr)
    saved = _in_window(url, articles.doi_of(args.article), out, args.session_dir, fetch,
                       keep_open=getattr(args, "keep_open", False))
    if not saved:
        print("No PDF saved.", file=sys.stderr)
        return 1
    return 0


def cmd_watch(args) -> int:
    """Fetch, as `get` does, each DOI added to wanted.txt in the output folder, until stopped.

    For an AI agent whose sandbox can't open a window: you start this in your own terminal,
    the agent adds DOIs to the list and reads the result from watch.log, one line per entry:
    "<doi><TAB><saved file>", or "no PDF" / "not a DOI" in place of the file. Only DOIs are
    taken from the list, and only their doi.org links are opened, so nothing written there
    can run or open another site.
    """
    if not _has_screen(args.cmd):
        return 1
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    wanted, log = out / WANTED, out / WATCH_LOG
    done = {line.split("\t", 1)[0].lower() for line in _lines(log)}
    print(f"[WATCH] Waiting for DOIs in {wanted}, one per line; results go to {log}. "
          "Ctrl+C stops.", file=sys.stderr)
    fetched, last = 0, 0.0
    try:
        while fetched < args.max:
            todo = [ref for ref in _lines(wanted) if _key(ref) not in done] if _settled(wanted) else []
            if not todo:
                time.sleep(WATCH_POLL_S)
                continue
            ref = todo[0]
            done.add(_key(ref))
            doi = articles.doi_of(ref)
            if not doi:
                result = "not a DOI"
            else:
                time.sleep(max(0.0, last + WATCH_PAUSE_S - time.monotonic()))
                print(f"[WATCH] {doi}", file=sys.stderr)
                saved = _in_window(articles.article_url(doi), doi, out, args.session_dir, fetch=True)
                fetched, last = fetched + 1, time.monotonic()
                result = str(saved[0].resolve()) if saved else "no PDF"
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{doi or ref}\t{result}\n")
    except KeyboardInterrupt:
        print("[WATCH] Stopped.", file=sys.stderr)
        return 0
    print(f"[WATCH] Fetched {fetched} articles, the most for one run (--max): "
          "start it again for more.", file=sys.stderr)
    return 0


def _key(ref: str) -> str:
    return (articles.doi_of(ref) or ref).lower()


def _lines(path: Path) -> list[str]:
    """The entries in a list file: its lines, without blank ones and # comments."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [s for s in (line.strip() for line in lines) if s and not s.startswith("#")]


def _settled(path: Path) -> bool:
    """Whether path exists and hasn't changed for a second, so it isn't read halfway a write."""
    try:
        return time.time() - path.stat().st_mtime >= 1
    except OSError:
        return False


def _has_screen(cmd: str) -> bool:
    if auth.has_screen():
        return True
    print(f"ERROR: `uu library {cmd}` needs a screen to open its window on "
          "(DISPLAY or WAYLAND_DISPLAY is not set).", file=sys.stderr)
    return False


def _in_window(url: str, doi: str | None, out: Path, session_dir: str, fetch: bool,
               keep_open: bool = False) -> list[Path]:
    with sync_playwright() as pw:
        ctx = browser.launch(pw, session_dir)
        try:
            return proxy.open_article(ctx, url, out, doi, keep_open=keep_open, fetch=fetch)
        finally:
            browser.save_session(ctx, session_dir)


def cmd_elsevier(args) -> int:
    """Download Elsevier articles through Elsevier's API."""
    key = os.environ.get("ELSEVIER_API_KEY", "")
    if not key:
        print("ERROR: set ELSEVIER_API_KEY to your API key from "
              "https://dev.elsevier.com/apikey/manage", file=sys.stderr)
        return 1
    token = os.environ.get("ELSEVIER_INSTTOKEN", "")
    out = Path(args.output)
    failed = 0
    for i, ref in enumerate(args.articles):
        doi = articles.doi_of(ref)
        if not doi:
            print(f"{ref}: not a DOI", file=sys.stderr)
            failed += 1
            continue
        if i:
            time.sleep(PAUSE_S)
        try:
            if elsevier.entitled(doi, key, token) is False:
                raise elsevier.ElsevierError(elsevier.NO_ACCESS)
            path = articles.save_pdf(out, elsevier.fetch_pdf(doi, key, token), doi)
        except elsevier.ElsevierError as err:
            print(f"{doi}: {err}", file=sys.stderr)
            failed += 1
            continue
        except OSError as err:  # no network, a proxy, TLS
            print(f"{doi}: could not reach Elsevier: {err}", file=sys.stderr)
            failed += 1
            continue
        print(f"Saved: {path}")
    return 1 if failed else 0


COMMANDS = {"open": cmd_open, "get": cmd_get, "watch": cmd_watch, "elsevier": cmd_elsevier}


def _window_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("article", help="A DOI (10.1016/…) or the article's web address")
    sp.add_argument("-o", "--output", default=".", help="Folder for the PDF (default: here)")
    sp.add_argument("--session-dir", default=str(paths.SESSION_DIR),
                    help="Browser profile, shared with the other services (one UU login)")


def main(argv: list[str] | None = None, prog: str | None = None) -> None:
    paths.ensure_state_dirs()
    p = argparse.ArgumentParser(
        prog=prog, description="Articles from journals UU subscribes to, through UU's library")
    sub = p.add_subparsers(dest="cmd", required=True)

    op = sub.add_parser("open", help="Open an article through UU's library in a browser window; "
                                     "the PDF you open or download there is saved")
    _window_args(op)
    op.add_argument("--keep-open", action="store_true",
                    help="Keep the window open after the first PDF, until you close it")

    ge = sub.add_parser("get", help="Like open, but the window opens the article's PDF itself; "
                                    "you only log in when UU asks")
    _window_args(ge)

    wa = sub.add_parser("watch", help=f"Fetch, as get does, every DOI added to {WANTED} in the "
                                      "output folder, for an AI agent that can't open a window "
                                      "itself; Ctrl+C stops")
    wa.add_argument("-o", "--output", default=".",
                    help=f"Folder for the PDFs, {WANTED} and {WATCH_LOG} (default: here)")
    wa.add_argument("--max", type=int, default=WATCH_MAX,
                    help=f"Stop after this many articles (default: {WATCH_MAX})")
    wa.add_argument("--session-dir", default=str(paths.SESSION_DIR),
                    help="Browser profile, shared with the other services (one UU login)")

    el = sub.add_parser("elsevier", help="Download Elsevier articles through Elsevier's API "
                                         "(ELSEVIER_API_KEY; full text from UU's network)")
    el.add_argument("articles", nargs="+", help="DOIs of Elsevier articles")
    el.add_argument("-o", "--output", default=".", help="Folder for the PDFs (default: here)")

    args = p.parse_args(argv)
    sys.exit(COMMANDS[args.cmd](args))
