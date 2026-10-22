"""Elsevier's Article Retrieval API: the route Elsevier allows for automated full-text access.

Its website terms forbid scripts that download from ScienceDirect; this API is the
alternative. A personal API key (dev.elsevier.com) identifies you but grants nothing by
itself: Elsevier gives full text when it recognises the request as UU's, by UU's network
address, or elsewhere by an institutional token that only the library can request.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import quote, urljoin

API = "https://api.elsevier.com/content/article"

NO_ACCESS = ("Elsevier doesn't count this request as UU's, so there's no full text. From UU's "
             "network (on campus or eduroam) it should work; elsewhere it needs an institutional "
             "token (ELSEVIER_INSTTOKEN), which only the library can request. "
             "`uu library open <doi>` works from anywhere.")
ERRORS = {
    400: "Elsevier didn't understand the request; is the DOI complete?",
    401: "Elsevier didn't accept the API key in ELSEVIER_API_KEY.",
    403: NO_ACCESS,
    404: "Elsevier has no article with this DOI (the API only has Elsevier's own journals).",
    429: "The API key's quota is used up; try again later.",
}


class ElsevierError(Exception):
    pass


def entitled(doi: str, key: str, insttoken: str = "") -> bool | None:
    """Whether Elsevier gives you this article's full text; None when it doesn't say."""
    status, _, body = _get(f"{API}/entitlement/doi/{quote(doi, safe='/')}",
                           _headers(key, insttoken, "application/json"))
    if status != 200:
        return None  # the PDF request that follows reports what's wrong
    try:
        value = _find(json.loads(body), "entitled")
    except ValueError:
        return None
    return None if value is None else str(value).lower() == "true"


def fetch_pdf(doi: str, key: str, insttoken: str = "") -> bytes:
    """The article's PDF; raises ElsevierError with what to do when Elsevier won't give it."""
    url = f"{API}/doi/{quote(doi, safe='/')}"
    status, headers, body = _get(url, _headers(key, insttoken, "application/pdf"))
    if status in (301, 302, 303, 307, 308) and headers.get("location"):
        # The PDF itself, elsewhere; the key and token are for Elsevier's API only.
        status, headers, body = _get(urljoin(url, headers["location"]), {"Accept": "application/pdf"})
    if status != 200:
        raise ElsevierError(ERRORS.get(status, f"Elsevier answered HTTP {status}."))
    if not body.startswith(b"%PDF"):
        raise ElsevierError("Elsevier answered without a PDF.")
    return body


def _headers(key: str, insttoken: str, accept: str) -> dict:
    headers = {"X-ELS-APIKey": key, "Accept": accept}
    if insttoken:
        headers["X-ELS-Insttoken"] = insttoken
    return headers


def _find(data, key: str):
    """The first value of key anywhere in data (Elsevier nests it a few levels deep)."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        data = list(data.values())
    if isinstance(data, list):
        for item in data:
            found = _find(item, key)
            if found is not None:
                return found
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect comes back as a response; fetch_pdf follows it without the key


_opener = urllib.request.build_opener(_NoRedirect)


def _get(url: str, headers: dict) -> tuple[int, dict, bytes]:
    """GET url; returns the status, the headers (lowercase names) and the body, also for errors."""
    try:
        with _opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in (err.headers or {}).items()}, err.read()
