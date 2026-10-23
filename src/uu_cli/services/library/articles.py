"""DOIs, article addresses and the PDF files saved for them."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

# A DOI: "10.", a registrant code, "/", and a suffix running up to whitespace, a query or a fragment.
DOI = re.compile(r"10\.\d{4,9}/[^\s?#]+")

# What a file name can't hold: folder separators, Windows' reserved characters and whitespace.
UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def doi_of(ref: str) -> str | None:
    """The DOI in ref (a bare DOI, doi:…, a doi.org link or a publisher link that has one), or None."""
    found = DOI.search(unquote(ref.strip()))
    # A PDF's address may end in the DOI plus ".pdf" (Springer: /content/pdf/<doi>.pdf).
    return found.group(0).rstrip(".,;").removesuffix(".pdf") if found else None


def article_url(ref: str) -> str:
    """Where the article lives: ref itself when it's a web address, else its doi.org link."""
    ref = ref.strip()
    if ref.lower().startswith(("http://", "https://")):
        return ref
    doi = doi_of(ref)
    if not doi:
        raise ValueError(f"Not a DOI or a web address: {ref}")
    return f"https://doi.org/{doi}"


def save_pdf(out_dir: Path, data: bytes, name: str) -> Path:
    """Write data to out_dir/<name>.pdf, or <name>-2.pdf and up when that holds another PDF.

    The same PDF twice (a download and the page it opened in) ends up in one file.
    """
    return save_file(out_dir, data, name, ".pdf")


def save_file(out_dir: Path, data: bytes, name: str, suffix: str) -> Path:
    """Write data to out_dir/<name><suffix>, or <name>-2<suffix> and up when that holds other data."""
    stem = UNSAFE.sub("_", name).strip("._") or "article"
    suffix = UNSAFE.sub("_", suffix)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        path = out_dir / (f"{stem}{suffix}" if n == 1 else f"{stem}-{n}{suffix}")
        if not path.exists():
            path.write_bytes(data)
            return path
        if path.read_bytes() == data:
            return path
        n += 1
