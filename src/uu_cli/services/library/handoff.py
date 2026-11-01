"""Your own browser, for a site that turns the library window away.

Cloudflare's bot check on ScienceDirect and IEEE Xplore's 418 refuse the window the CLI drives,
but let your everyday browser through: on 29-10-2026 six "View PDF" clicks in the window got a
403, and the same click in Firefox gave the PDF. So with --pickup the window hands the article
over: it opens the proxy's link in your own browser, and takes the PDF you save into the pickup
folder from there. Nothing is disguised: the site sees your own browser, as on any other day.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from uu_cli.services.library.articles import save_pdf

# What opens a link in your own browser: WSL's helper, Windows' explorer from inside WSL, a
# Linux desktop's default. The first one found is used.
OPENERS = ("wslview", "explorer.exe", "xdg-open")

# How often the pickup folder is looked at, how long a new file must stay unchanged before it
# counts as saved (a browser writes it in parts), and how long the hand-off waits.
PICKUP_POLL_S = 2.0
SETTLED_S = 1.0
PICKUP_TIMEOUT_S = 900


def hand_off(link: str, doi: str, out: Path, pickup: Path) -> Path | None:
    """Open link in your own browser and wait for the PDF that carries doi in pickup; it's moved
    to out under the DOI's name. None when it doesn't come within PICKUP_TIMEOUT_S."""
    since = time.time() - 2  # a file system's clock can trail this one by a few milliseconds
    how = "your own browser opens it" if _open(link) else "open it in your own browser"
    print(f"[LIBRARY] This site turns the window away, so {how}: {link}", file=sys.stderr)
    print(f"[LIBRARY] Save the PDF into {pickup}/ and it's taken from there "
          f"(waiting {PICKUP_TIMEOUT_S // 60} minutes; Ctrl+C stops).", file=sys.stderr)
    said: set[Path] = set()
    deadline = time.monotonic() + PICKUP_TIMEOUT_S
    while time.monotonic() < deadline:
        found = pick_up(pickup, doi, since, said)
        if found:
            path = save_pdf(out, found.read_bytes(), doi)
            if path.resolve() != found.resolve():
                found.unlink()
            return path
        time.sleep(PICKUP_POLL_S)
    print(f"[LIBRARY] No PDF with {doi} came into {pickup}/.", file=sys.stderr)
    return None


def pick_up(folder: Path, doi: str, since: float, said: set[Path] | None = None) -> Path | None:
    """The newest PDF saved in folder since `since` that carries doi, once the browser is done
    writing it. A publisher's PDF carries its own DOI in its metadata, which isn't compressed
    (ScienceDirect's do, see proxy._article_in_zip); any other new file is named once in said and
    left where it is."""
    needle = doi.lower().encode()
    try:
        pdfs = sorted(folder.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for pdf in pdfs:
        try:
            changed = pdf.stat().st_mtime
            if changed < since or time.time() - changed < SETTLED_S:
                continue
            data = pdf.read_bytes()
        except OSError:
            continue  # moved or still being written
        if data.startswith(b"%PDF") and needle in data.lower():
            return pdf
        if said is not None and pdf not in said:
            said.add(pdf)
            print(f"[LIBRARY] {pdf.name} came in, but it isn't the PDF of {doi}: left where it is.",
                  file=sys.stderr)
    return None


def _open(link: str) -> bool:
    """Open link with the first opener there is; False when there is none."""
    for name in OPENERS:
        program = shutil.which(name)
        if program:
            subprocess.Popen([program, link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False
