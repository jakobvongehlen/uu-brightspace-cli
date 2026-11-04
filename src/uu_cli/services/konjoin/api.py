"""Konjoin's JSON API (a Laravel app), called from a page on its own origin so the session cookie goes along."""

from __future__ import annotations

import json
from collections.abc import Iterator
from urllib.parse import urlencode, urlparse

from playwright.sync_api import BrowserContext, Page

from uu_cli import env

BASE_URL = env.get("UU_KONJOIN_URL", "https://uu.konjoin.nl").rstrip("/")
# The identity provider behind the site's own login button: SURFconext, then UU's Solis-id page.
IDENTITY_PROVIDER = env.get("UU_KONJOIN_IDENTITY_PROVIDER", "2")
LOGIN_URL = f"{BASE_URL}/saml2/login?identity_provider={IDENTITY_PROVIDER}"


class NotLoggedIn(Exception):
    """Konjoin wants a login for this request."""


def current_user(ctx: BrowserContext) -> str | None:
    """The logged-in user's name, or None for a guest; read from the meta tags on every page."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=30_000)
    return page.evaluate("""() => {
        const meta = name => document.querySelector(`meta[name="${name}"]`)?.content || null;
        return meta("user-id") && (meta("user-name") || meta("user-id"));
    }""")


def get_json(ctx: BrowserContext, path: str, params: dict | None = None):
    """GET an /api/ path. Raises NotLoggedIn when the request needs a login."""
    url = BASE_URL + path + (f"?{urlencode(params)}" if params else "")
    result = _konjoin_page(ctx).evaluate("""async (url) => {
        const r = await fetch(url, {headers: {"Accept": "application/json",
                                              "X-Requested-With": "XMLHttpRequest"}});
        const loginWall = r.status === 401 || r.status === 419
            || (r.redirected && new URL(r.url).pathname === "/login");
        return {status: r.status, loginWall, body: loginWall ? null : await r.text()};
    }""", url)
    if result["loginWall"]:
        raise NotLoggedIn(path)
    if not 200 <= result["status"] < 300:
        raise RuntimeError(f"Konjoin {path}: HTTP {result['status']}")
    return json.loads(result["body"])


def project_pages(ctx: BrowserContext, query: str | None = None, page: int = 1,
                  all_pages: bool = False) -> Iterator[dict]:
    """Pages of /api/project (Laravel pagination: data, current_page, last_page, total)."""
    while True:
        params = {"page": page, **({"query": query} if query else {})}
        data = get_json(ctx, "/api/project", params)
        yield data
        if not all_pages or data.get("current_page", page) >= data.get("last_page", page):
            return
        page += 1


def _konjoin_page(ctx: BrowserContext) -> Page:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if urlparse(page.url).hostname != urlparse(BASE_URL).hostname:
        page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=30_000)
    return page
