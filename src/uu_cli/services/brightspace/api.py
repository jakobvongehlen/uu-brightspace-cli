"""The Brightspace bearer token and authenticated calls to /d2l/api/."""

from __future__ import annotations

import base64
import json
import sys
import time
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext

from uu_cli import auth, env, paths
from uu_cli.login import log_in

BASE_URL  = env.get("UU_BRIGHTSPACE_URL", "https://uu.brightspace.com").rstrip("/")


def load_token() -> dict | None:
    if not paths.TOKEN_FILE.exists():
        return None
    try:
        return json.loads(paths.TOKEN_FILE.read_text())
    except Exception:
        return None


def save_token(token: str) -> None:
    paths.write_private(paths.TOKEN_FILE, json.dumps({
        "token": token,
        "savedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiresIn": 28800,
    }))


EXPIRED_MSG = ("ERROR: Brightspace no longer takes the token. "
               "Run `uu login brightspace` to get a new one.")


def token_expired(token: str, margin: int = 60) -> bool:
    """True if the token's JWT 'exp' claim is in the past (or within margin seconds)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims["exp"] <= time.time() + margin
    except Exception:
        return False  # not a JWT we can read; let the API decide


def extract_token(ctx: BrowserContext) -> str | None:
    """
    Pull D2L.Fetch.Tokens from localStorage via Playwright.
    Handles cross-origin restrictions (skip pages where localStorage is inaccessible).
    Expired tokens left over in localStorage are ignored.
    """
    tok = _extract_token(ctx)
    return tok if tok and not token_expired(tok) else None


def _extract_token(ctx: BrowserContext) -> str | None:
    for page in ctx.pages:
        try:
            raw = page.evaluate(
                "localStorage.getItem('D2L.Fetch.Tokens') || ''"
            )
        except Exception:
            continue
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for v in parsed.values():
                    if isinstance(v, dict) and v.get("access_token"):
                        return v["access_token"]
                if parsed.get("access_token"):
                    return parsed["access_token"]
        except Exception:
            pass
    return None


def api_fetch(ctx: BrowserContext, url: str, token: str,
              timeout: float = 20000) -> Optional[Any]:
    """GET a Brightspace /d2l/api/ endpoint via Playwright page JS (carries cookies)."""
    page = ctx.pages[0]
    try:
        data = page.evaluate(f"""
            async () => {{
                const r = await fetch(
                    "{url}",
                    {{ headers: {{ "Authorization": "Bearer {token}",
                                   "Accept": "application/json" }} }}
                );
                if (r.status === 401) return {{ "__unauthorized__": true }};
                const text = await r.text();
                if (r.status === 204 || !text.trim()) return null;
                try {{
                    return JSON.parse(text);
                }} catch {{
                    return null;
                }}
            }}
        """)
    except Exception as e:
        print(f"  ! API {url}: {e}", file=sys.stderr)
        return None
    if isinstance(data, dict) and data.get("__unauthorized__"):
        sys.exit(EXPIRED_MSG)
    return data


def enrolled_courses(ctx: BrowserContext, token: str) -> list[tuple[str, dict]] | None:
    """(org-unit id, OrgUnit) of every course offering you're enrolled in, without group or team
    sections; None when the enrollments can't be fetched."""
    data = api_fetch(ctx, f"{BASE_URL}/d2l/api/lp/1.43/enrollments/myenrollments/", token)
    items = data.get("Items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
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
    return courses


def get_token(ctx: BrowserContext, args, session: bool = False) -> str | None:
    """A live bearer token for /d2l/api/, logging in when there is none; None after saying why not.

    In order: --token, the cached token, a new one from the saved browser session, one from a
    new login. session: make sure this browser is logged in too, as content pages
    (/content/enforced/*) need the session's cookies and don't take the token (sync).
    """
    if not session and args.token:
        return args.token
    cached = (load_token() or {}).get("token")
    expired = bool(cached) and token_expired(cached)
    if cached and not expired and not session:
        return cached
    if _at_home(ctx):  # the saved session still works: it gives a new token without a login
        token = extract_token(ctx)
        if token:
            save_token(token)
            print("[AUTH] Token refreshed from the saved session.", file=sys.stderr)
            return token
        if cached and not expired:
            return cached
    if not auth.ready_to_login(args):
        why = "The Brightspace token expired" if expired else "No Brightspace session"
        print(f"ERROR: {why}, and {auth.CANNOT_LOGIN}.", file=sys.stderr)
        return None
    log_in(ctx, BASE_URL, args)
    token = extract_token(ctx) if _at_home(ctx) else None
    if not token:
        print("ERROR: Logged in, but Brightspace gave no token.", file=sys.stderr)
        return None
    save_token(token)
    return token


def _at_home(ctx: BrowserContext) -> bool:
    """Open Brightspace's home page; False when it sends you to log in instead."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(f"{BASE_URL}/d2l/home", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
    except Exception:
        return False
    return urlparse(page.url).hostname == urlparse(BASE_URL).hostname
