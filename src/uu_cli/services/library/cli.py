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


def cmd_link(args) -> int:
    """Print each article's link through UU's library, to open in your own browser.

    No window and no login here: the browser you open it in logs in when UU asks, so this also
    works where a window can't open, such as over SSH.
    """
    failed = 0
    for ref in args.articles:
        try:
            print(proxy.proxy_url(articles.article_url(ref)))
        except ValueError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            failed += 1
    return 2 if failed else 0


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
    saved, _ = _in_window(url, articles.doi_of(args.article), out, args.session_dir, fetch,
                          keep_open=getattr(args, "keep_open", False))
    if not saved:
        print("No PDF saved.", file=sys.stderr)
        return 1
    return 0


def cmd_watch(args) -> int:
    """Fetch, as `get` does, each DOI added to wanted.txt in the output folder, until stopped.

    A reading list: you leave this running in a terminal, add DOIs to the list whenever you
    come across one, and find the result in watch.log, one line per entry:
    "<doi><TAB><saved file>", or "no PDF" / "not a DOI" in place of the file; "no PDF; kept
    <files>" names what was downloaded in the window that isn't a PDF. Only DOIs are
    taken from the list, and only their doi.org links are opened, so nothing written there
    can run or open another site.

    Each entry it takes says how far along the list it is ("2/5"), and once the list is
    through it says "Done", with how many came with a PDF, and keeps waiting for more.
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
    taken, with_pdf, reported = 0, 0, 0  # this run: entries taken, how many got a PDF, said Done at
    try:
        while True:
            settled = _settled(wanted)
            todo = [ref for ref in _lines(wanted) if _key(ref) not in done] if settled else []
            if settled and not todo and taken > reported:
                more = "" if fetched >= args.max else " Waiting for more DOIs; Ctrl+C stops."
                print(f"[WATCH] Done: {with_pdf} of {taken} with a PDF; {log} has each result.{more}",
                      file=sys.stderr)
                reported = taken
            if fetched >= args.max:
                break
            if not todo:
                time.sleep(WATCH_POLL_S)
                continue
            ref = todo[0]
            done.add(_key(ref))
            step = f"{taken + 1}/{taken + len(todo)}"
            taken += 1
            doi = articles.doi_of(ref)
            if not doi:
                print(f"[WATCH] {step} {_shown(ref)}: not a DOI", file=sys.stderr)
                result = "not a DOI"
            else:
                time.sleep(max(0.0, last + WATCH_PAUSE_S - time.monotonic()))
                print(f"[WATCH] {step} {doi}", file=sys.stderr)
                saved, others = _in_window(articles.article_url(doi), doi, out, args.session_dir,
                                           fetch=True)
                fetched, last = fetched + 1, time.monotonic()
                with_pdf += bool(saved)
                result = str(saved[0].resolve()) if saved else "no PDF"
                if others and not saved:
                    result += "; kept " + ", ".join(str(p.resolve()) for p in others)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{doi or ref}\t{result}\n")
    except KeyboardInterrupt:
        print("[WATCH] Stopped.", file=sys.stderr)
        return 0
    if any(_key(ref) not in done for ref in _lines(wanted)):
        print(f"[WATCH] Fetched {fetched} articles, the most for one run (--max): "
              "start it again for more.", file=sys.stderr)
    return 0


def _shown(ref: str) -> str:
    """A list entry as the terminal may show it: printable characters only, so a line in the
    list can't send the terminal escape codes."""
    return "".join(c if c.isprintable() else "?" for c in ref)[:100]


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
               keep_open: bool = False) -> tuple[list[Path], list[Path]]:
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


COMMANDS = {"open": cmd_open, "get": cmd_get, "link": cmd_link, "watch": cmd_watch,
            "elsevier": cmd_elsevier}


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

    li = sub.add_parser("link", help="Print the link through UU's library for each article, to "
                                     "open in your own browser; no window, no login here")
    li.add_argument("articles", nargs="+", help="DOIs (10.1016/…) or the articles' web addresses")

    wa = sub.add_parser("watch", help=f"Fetch, as get does, every DOI added to {WANTED} in the "
                                      "output folder, a reading list; Ctrl+C stops")
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
