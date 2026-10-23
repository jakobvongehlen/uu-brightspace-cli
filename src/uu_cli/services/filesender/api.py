"""SURFfilesender's REST API, called with your login session, as its own upload page does.

SURF doesn't hand out API secrets (its server has FileSender's "remote authentication" off),
so the CLI calls the REST API from the logged-in browser profile: the session cookie goes
along, with the page's security token, and the CSRF token on all but a GET, as the
website's own client.js sends them. A transfer is a POST /transfer with the files' names and
sizes, the file chunks as PUTs, then a PUT that completes each file and one that completes
the transfer.

With a password, chunks are encrypted as the website does it: AES-GCM with a key derived
(PBKDF2-SHA256) from the password and the transfer's salt, and a nonce of the file's IV plus the
chunk number. Recipients type the password on the download page.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE_URL = os.environ.get("UU_FILESENDER_URL", "https://filesender.surf.nl").rstrip("/")
REST = f"{BASE_URL}/rest.php"
# The client settings the website's own pages read (chunk size, encryption, limits).
CLIENT_CONFIG = f"{BASE_URL}/filesender-config.js.php"
# The upload page: it carries the session's security token.
UPLOAD = f"{BASE_URL}/?s=upload"
LOGIN = f"{BASE_URL}/simplesaml/module.php/saml/sp/login/default-sp?ReturnTo={UPLOAD}"

TIMEOUT_S = 120


class FilesenderError(Exception):
    """SURFfilesender refused, with its own message where it gave one."""


class NotLoggedIn(FilesenderError):
    """The session has expired, or there was none."""


# One HTTP request: (method, url, headers, body) -> (status, response headers, body).
Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, dict, bytes]"]


@dataclass
class Session:
    send: Transport  # through the logged-in browser profile (commands.connected), or a fake
    token: str = ""  # the page's data-security-token, which the server may renew
    csrf: str = ""   # the csrfptoken cookie, which every request but a GET repeats as a header


def call(session: Session, method: str, path: str, params: dict | None = None,
         content=None, raw: bytes | None = None):
    """A request to the REST API with the session; returns its JSON, or raises FilesenderError."""
    body = json.dumps(content, separators=(",", ":")).encode() if content is not None else raw
    headers = {"Accept": "application/json",
               "Content-Type": "application/octet-stream" if raw is not None else "application/json"}
    if session.token:
        headers["X-Filesender-Security-Token"] = session.token
    if method.lower() != "get" and session.csrf:  # client.js and csrfprotector.js add it
        headers["csrfptoken"] = session.csrf
    url = f"{REST}{path}" + (f"?{urlencode(params)}" if params else "")
    status, got, data = session.send(method.upper(), url, headers, body)
    session.token = {k.lower(): v for k, v in got.items()}.get(
        "x-filesender-security-token") or session.token
    if status not in (200, 201):
        raise _error(status, data)
    try:
        return json.loads(data) if data else None
    except ValueError:
        raise FilesenderError(f"HTTP {status}, but not the JSON the API answers with") from None


def _error(status: int, data: bytes) -> FilesenderError:
    try:
        error = json.loads(data)
        text = error.get("message", "") if isinstance(error, dict) else ""
    except ValueError:
        text = ""
    if text in ("rest_authentication_required", "auth_user_not_allowed"):
        return NotLoggedIn("not logged in (any more); run `uu filesender login`")
    if text == "rest_xsrf_token_did_not_match":
        return FilesenderError("the session's security token didn't match; try again")
    return FilesenderError(f"HTTP {status}" + (f": {text}" if text else ""))


def _get_public(session: Session, url: str) -> bytes:
    status, _, data = session.send("GET", url, {"Accept": "*/*"}, None)
    if status != 200:
        raise FilesenderError(f"{url}: HTTP {status}")
    return data


def chunk_size(session: Session) -> int:
    return int(json.loads(_get_public(session, f"{REST}/info"))["upload_chunk_size"])


# ── encryption, as the website and filesender.py do it ───────────────────────

@dataclass
class Crypto:
    iv_len: int
    iterations: int
    key_version: str = "3"  # AES-GCM


def crypto_settings(session: Session, password: str) -> Crypto:
    """The server's encryption settings, after checking password against its rules."""
    text = _get_public(session, CLIENT_CONFIG).decode("utf-8", "replace")

    def setting(name: str, pattern: str = r"\W*(\w+)") -> str:
        found = re.search(name + pattern, text)
        if not found:
            raise FilesenderError(f"the server's settings don't say {name}; can't encrypt")
        return found.group(1)

    if (setting("crypto_crypt_name", r": '(.+?)'"), setting("crypto_hash_name", r": '(.+?)'")) != (
            "AES-GCM", "SHA-256"):
        raise FilesenderError("the server encrypts differently than AES-GCM with SHA-256")
    if not password.isascii():
        raise FilesenderError("the password must be plain ASCII")
    rules = [("encryption_password_must_have_upper_and_lower_case",
              any(c.islower() for c in password) and any(c.isupper() for c in password),
              "upper and lower case"),
             ("encryption_password_must_have_numbers", any(c.isdigit() for c in password), "a number"),
             ("encryption_password_must_have_special_characters",
              not password.isalnum(), "a character other than a letter or number")]
    for name, ok, what in rules:
        if setting(name) == "true" and not ok:
            raise FilesenderError(f"the password needs {what}")
    return Crypto(iv_len=int(setting("crypto_iv_len", r"\D+(\d+)")),
                  iterations=int(setting("encryption_password_hash_iterations_new_files", r"\D+(\d+)")))


def file_crypto(crypto: Crypto, size: int, chunk: int) -> tuple[bytes, str]:
    """A file's random IV and its AEAD descriptor, exactly as filesender.py writes it."""
    iv = os.urandom(crypto.iv_len - 4)
    iv64 = base64.b64encode(iv).decode("ascii")
    aead = (f'{{"aeadversion":1,"chunkcount":{ceil(size / chunk)},"chunksize":{chunk},'
            f'"iv":"{iv64}","aeadterminator":1}}')
    return iv, aead


def derive_key(password: str, salt: str, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt.encode("ascii"), iterations, 32)


def encrypt_chunk(key: bytes, iv: bytes, aead: str, index: int, data: bytes) -> bytes:
    nonce = iv + index.to_bytes(4, byteorder="little")
    return nonce + AESGCM(key).encrypt(nonce, data, aead.encode("ascii"))


# ── a transfer ─────────────────────────────────────────────────────────────────

def send(session: Session, files: list[Path], recipients: list[str], subject: str | None = None,
         message: str | None = None, days: int | None = None, password: str | None = None,
         progress=lambda text: None) -> dict:
    """Upload files to recipients; returns the transfer as the server describes it.

    Without days the server's default applies (14 days at SURF, at most 21). A transfer that
    fails halfway is deleted again, so no recipient gets half of it.
    """
    chunk = chunk_size(session)
    crypto = crypto_settings(session, password) if password else None
    local, described = {}, []
    for path in files:
        size = path.stat().st_size
        key = f"{path.name}:{size}"
        if key in local:
            raise FilesenderError(f"two files named {path.name} of the same size; rename one")
        local[key] = {"path": path}
        entry = {"name": path.name, "size": size}
        if crypto:
            iv, aead = file_crypto(crypto, size, chunk)
            local[key].update(iv=iv, aead=aead)
            entry.update(iv=base64.b64encode(iv).decode("ascii"),
                         aead=base64.b64encode(aead.encode("ascii")).decode("ascii"))
        described.append(entry)

    content = {
        "from": None,  # the server takes your account's email address
        "files": described,
        "recipients": recipients,
        "subject": subject,
        "message": message,
        "aup_checked": 1,  # you accepted the code of conduct (`send --accept-terms`)
        "options": {"get_a_link": 0},
    }
    if days:
        # A minute short of the full days, so a clock that runs ahead can't cross the maximum.
        content["expires"] = round(time.time()) + days * 86400 - 60
    if crypto:
        content.update(encryption=True, encryption_key_version=crypto.key_version,
                       encryption_password_encoding="none", encryption_password_version="1",
                       encryption_password_hash_iterations=crypto.iterations)

    transfer = call(session, "post", "/transfer", content=content)
    token = transfer.get("roundtriptoken", "")
    first = transfer["files"][0]["puid"]
    try:
        key = derive_key(password, transfer["salt"], crypto.iterations) if crypto else None
        for f in transfer["files"]:
            if int(f["size"]) == 0:
                continue  # nothing to upload, and filesender.py doesn't complete one either
            mine = local[f"{f['name']}:{f['size']}"]
            ticket = {"key": f["puid"], "roundtriptoken": token}
            with open(mine["path"], "rb") as fh:
                for index, offset in enumerate(range(0, int(f["size"]), chunk)):
                    data = fh.read(chunk)
                    if key:
                        data = encrypt_chunk(key, mine["iv"], mine["aead"], index, data)
                    call(session, "put", f"/file/{f['id']}/chunk/{offset}", ticket, raw=data)
                    progress(f"{f['name']}: {min(offset + chunk, int(f['size']))} of {f['size']} bytes")
            call(session, "put", f"/file/{f['id']}", ticket, content={"complete": True})
        call(session, "put", f"/transfer/{transfer['id']}", {"key": first}, content={"complete": True})
    except BaseException:
        try:
            call(session, "delete", f"/transfer/{transfer['id']}", {"key": first})
        except Exception:
            pass  # the transfer was already broken; the server expires it
        raise
    return transfer


def transfers(session: Session) -> list[dict]:
    """Your transfers that haven't expired."""
    return call(session, "get", "/transfer/@me") or []
