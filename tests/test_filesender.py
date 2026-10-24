"""SURFfilesender: the REST calls with the login session against a fake server, and the login in a real Chromium."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fake_uu import FILESENDER, FILESENDER_CSRF, FILESENDER_TOKEN, PASSWORD, SOLISID, TOTP_SECRET
from uu_cli import auth
from uu_cli.services.brightspace.cli import main as brightspace
from uu_cli.services.filesender import api, commands
from uu_cli.services.filesender.cli import main as filesender

CONFIG_JS = """window.filesender.config = {
    crypto_crypt_name: 'AES-GCM',
    crypto_hash_name: 'SHA-256',
    crypto_iv_len: '16',
    encryption_password_hash_iterations_new_files: '1000',
    encryption_password_must_have_upper_and_lower_case: true,
    encryption_password_must_have_numbers: true,
    encryption_password_must_have_special_characters: true,
};"""


class FakeFilesender:
    """The REST API, refusing a request without the session the way FileSender and csrfprotector do."""

    def __init__(self, logged_in=lambda: True):
        self.logged_in = logged_in  # whether the request carries a session cookie
        self.chunk = 4
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        self.headers: list[dict] = []
        self.transfers: dict[int, dict] = {}
        self.chunks: dict[int, dict[int, bytes]] = {}
        self.files_done: list[int] = []
        self.completed: list[int] = []
        self.deleted: list[int] = []
        self.fail_chunk: tuple[int, int] | None = None

    def __call__(self, method, url, headers, body):
        status, data = self._answer(method, url, headers, body)
        return status, {"Content-Type": "application/json"}, data

    def _answer(self, method, url, headers, body):
        u = urlparse(url)
        if u.path == "/rest.php/info":
            return 200, json.dumps({"upload_chunk_size": self.chunk}).encode()
        if u.path == "/filesender-config.js.php":
            return 200, CONFIG_JS.encode()
        path = u.path.removeprefix("/rest.php")
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self.logged_in():
            return 403, json.dumps({"message": "rest_authentication_required"}).encode()
        if method != "GET" and (headers.get("X-Filesender-Security-Token") != FILESENDER_TOKEN
                                or headers.get("csrfptoken") != FILESENDER_CSRF):
            return 403, json.dumps({"message": "rest_xsrf_token_did_not_match"}).encode()
        self.requests.append((method, path, params, body))
        self.headers.append(headers)
        return self._route(method, path.strip("/").split("/"), params, body)

    def _route(self, method, parts, params, body):
        data = json.loads(body) if body and parts[-2:-1] != ["chunk"] else None
        if (method, parts) == ("POST", ["transfer"]):
            tid = len(self.transfers) + 1
            files = [{"id": 100 * tid + i, "puid": f"puid-{tid}-{i}", "name": f["name"], "size": f["size"]}
                     for i, f in enumerate(data["files"])]
            self.transfers[tid] = {
                "id": tid, "files": files, "roundtriptoken": "rt-token", "salt": "c2FsdHNhbHQ=",
                "expires": {"raw": 1, "formatted": "05/11/2026"}, "sent": data,
                "recipients": [{"email": r} for r in data["recipients"]],
            }
            return 201, json.dumps(self.transfers[tid]).encode()
        if parts[0] == "file":
            fid = int(parts[1])
            f = next(f for t in self.transfers.values() for f in t["files"] if f["id"] == fid)
            if (params.get("key"), params.get("roundtriptoken")) != (f["puid"], "rt-token"):
                return 403, json.dumps({"message": "rest_access_forbidden"}).encode()
            if len(parts) == 4:  # file/<id>/chunk/<offset>
                if self.fail_chunk == (fid, int(parts[3])):
                    return 500, json.dumps({"message": "storage_filesystem_cannot_write"}).encode()
                self.chunks.setdefault(fid, {})[int(parts[3])] = body
                return 200, b"true"
            self.files_done.append(fid)
            return 200, b"true"
        if parts[0] == "transfer" and parts[1] == "@me" and method == "GET":
            return 200, json.dumps([t for i, t in self.transfers.items() if i in self.completed]).encode()
        if parts[0] == "transfer":
            tid = int(parts[1])
            if params.get("key") != self.transfers[tid]["files"][0]["puid"]:
                return 403, json.dumps({"message": "rest_access_forbidden"}).encode()
            (self.deleted if method == "DELETE" else self.completed).append(tid)
            return 200, b"true"
        return 404, json.dumps({"message": "rest_endpoint_missing"}).encode()

    def sent(self, tid=1) -> dict:
        return self.transfers[tid]["sent"]

    def uploaded(self, fid: int) -> bytes:
        return b"".join(chunk for _, chunk in sorted(self.chunks[fid].items()))


@pytest.fixture
def fs(monkeypatch) -> FakeFilesender:
    """The fake behind a logged-in session, as `connected` gives it after the upload page."""
    fake = FakeFilesender()

    @contextmanager
    def connected(args):
        yield api.Session(send=fake, token=FILESENDER_TOKEN, csrf=FILESENDER_CSRF)

    monkeypatch.setattr(commands, "connected", connected)
    return fake


@pytest.fixture
def file(tmp_path):
    path = tmp_path / "interview-notes.txt"
    path.write_bytes(b"hello world!")  # 12 bytes: three chunks of four
    return path


def run(main, argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out, err = capsys.readouterr()
    return exc.value.code, out, err


def test_send_uploads_in_chunks_with_the_sessions_tokens(fs, file, capsys):
    code, out, err = run(filesender, ["send", str(file), "--to", "joris@example.org", "--accept-terms"], capsys)

    assert code == 0, err
    assert [(m, p) for m, p, _, _ in fs.requests] == [
        ("POST", "/transfer"), ("PUT", "/file/100/chunk/0"), ("PUT", "/file/100/chunk/4"),
        ("PUT", "/file/100/chunk/8"), ("PUT", "/file/100"), ("PUT", "/transfer/1")]
    assert fs.uploaded(100) == b"hello world!"
    assert all(h["X-Filesender-Security-Token"] == FILESENDER_TOKEN for h in fs.headers)
    assert all(h["csrfptoken"] == FILESENDER_CSRF for h in fs.headers)
    assert fs.files_done == [100] and fs.completed == [1] and fs.deleted == []
    sent = fs.sent()
    assert sent["recipients"] == ["joris@example.org"] and sent["aup_checked"] == 1
    assert sent["from"] is None and "expires" not in sent and "encryption" not in sent
    assert sent["files"] == [{"name": "interview-notes.txt", "size": 12}]
    assert out == "Sent 1 file to joris@example.org; available until 05/11/2026.\n"
    assert "Not encrypted" in err


def test_send_needs_the_code_of_conduct_accepted(fs, file, capsys):
    code, out, err = run(filesender, ["send", str(file), "--to", "joris@example.org"], capsys)

    assert code == 2 and out == ""
    assert "code of conduct" in err and "Huisregels+Code+of+Conduct" in err
    assert fs.requests == []


def test_recipients_by_comma_or_repeated_and_the_days(fs, file, capsys):
    code, _, err = run(filesender, ["send", str(file), "--to", "a@example.org, b@example.org",
                                    "--to", "c@example.org", "--days", "7", "--accept-terms"], capsys)

    assert code == 0, err
    sent = fs.sent()
    assert sent["recipients"] == ["a@example.org", "b@example.org", "c@example.org"]
    assert abs(sent["expires"] - (time.time() + 7 * 86400 - 60)) < 5


def test_a_failed_chunk_deletes_the_transfer(fs, file, capsys):
    fs.fail_chunk = (100, 4)

    code, out, err = run(filesender, ["send", str(file), "--to", "joris@example.org", "--accept-terms"], capsys)

    assert code == 1 and out == ""
    assert "HTTP 500: storage_filesystem_cannot_write" in err
    assert fs.deleted == [1] and fs.completed == []


def test_a_stale_security_token_says_so(fs, file, monkeypatch, capsys):
    @contextmanager
    def connected(args):
        yield api.Session(send=fs, token="an-old-token", csrf=FILESENDER_CSRF)
    monkeypatch.setattr(commands, "connected", connected)

    code, _, err = run(filesender, ["send", str(file), "--to", "joris@example.org", "--accept-terms"], capsys)

    assert code == 1 and "security token didn't match" in err
    assert fs.transfers == {}


def test_encrypted_chunks_open_with_the_password(fs, file, monkeypatch, capsys):
    monkeypatch.setenv("UU_FILESENDER_PASSWORD", "Geheim-123")

    code, out, err = run(filesender, ["send", str(file), "--to", "joris@example.org", "--encrypt",
                                      "--accept-terms"], capsys)

    assert code == 0, err
    sent = fs.sent()
    assert (sent["encryption"], sent["encryption_key_version"], sent["encryption_password_hash_iterations"]) == (
        True, "3", 1000)
    entry = sent["files"][0]
    iv = base64.b64decode(entry["iv"])
    aead = base64.b64decode(entry["aead"])
    assert json.loads(aead) == {"aeadversion": 1, "chunkcount": 3, "chunksize": 4,
                                "iv": entry["iv"], "aeadterminator": 1}
    key = hashlib.pbkdf2_hmac("sha256", b"Geheim-123", b"c2FsdHNhbHQ=", 1000, 32)
    plain = b""
    for index, (_, chunk) in enumerate(sorted(fs.chunks[100].items())):
        nonce = chunk[:16]
        assert nonce == iv + index.to_bytes(4, "little")
        plain += AESGCM(key).decrypt(nonce, chunk[16:], aead)
    assert plain == b"hello world!"
    assert "Not encrypted" not in err and "not by email" in err


def test_the_password_follows_the_servers_rules(fs, file, monkeypatch, capsys):
    monkeypatch.setenv("UU_FILESENDER_PASSWORD", "geheim")

    code, _, err = run(filesender, ["send", str(file), "--to", "a@example.org", "--encrypt",
                                    "--accept-terms"], capsys)

    assert code == 1 and "needs upper and lower case" in err
    assert fs.transfers == {}


def test_encrypt_without_a_password_or_a_terminal(fs, file, capsys):
    code, _, err = run(filesender, ["send", str(file), "--to", "a@example.org", "--encrypt",
                                    "--accept-terms"], capsys)

    assert code == 1 and "UU_FILESENDER_PASSWORD" in err
    assert fs.requests == []


def test_transfers_lists_yours(fs, file, capsys):
    run(filesender, ["send", str(file), "--to", "joris@example.org", "--accept-terms"], capsys)

    code, out, err = run(filesender, ["transfers"], capsys)

    assert code == 0, err
    assert out == "       1  until 05/11/2026  interview-notes.txt  → joris@example.org\n"


def test_a_folder_is_not_sent(fs, tmp_path, capsys):
    code, _, err = run(filesender, ["send", str(tmp_path), "--to", "a@example.org", "--accept-terms"], capsys)

    assert code == 2 and "zip it first" in err
    assert fs.requests == []


def test_a_session_that_ran_out_midway_says_so(fs, file, capsys):
    fs.logged_in = lambda: False

    code, _, err = run(filesender, ["send", str(file), "--to", "a@example.org", "--accept-terms"], capsys)

    assert code == 1 and "not logged in" in err and "uu filesender login" in err


# ── login, in a real Chromium against the offline fake ────────────────────────

@pytest.fixture
def at_terminal(monkeypatch):
    """Someone at the terminal reading codes off their phone; returns the prompts they saw."""
    prompts: list[str] = []

    def answer(prompt=""):
        prompts.append(prompt)
        return SOLISID if prompt.startswith("Solis-id") else pyotp.TOTP(TOTP_SECRET).now()

    monkeypatch.setattr(auth, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or PASSWORD)
    return prompts


@pytest.fixture
def through_the_browser(monkeypatch) -> FakeFilesender:
    """The REST fake behind the CLI's browser transport, logged in when the profile has the cookie."""
    def transport(ctx):
        fake.logged_in = lambda: any(c["name"] == "SimpleSAMLAuthToken" for c in ctx.cookies(FILESENDER))
        return fake
    fake = FakeFilesender()
    monkeypatch.setattr(commands, "_browser_transport", transport)
    return fake


@pytest.mark.browser
def test_login_through_surfconext_and_the_terms_page(fake_uu, through_the_browser, at_terminal, capsys):
    code, _, err = run(filesender, ["login"], capsys)

    assert code == 0, err
    assert "SURFconext: choosing Utrecht University" in err
    assert fake_uu.filesender_logins == 1
    assert fake_uu.filesender_terms and "Accepting \"SURFfilesender gebruiksvoorwaarden\"" in err


@pytest.mark.browser
def test_send_logs_in_and_uploads_with_the_pages_tokens(fake_uu, through_the_browser, at_terminal,
                                                       file, capsys):
    code, out, err = run(filesender, ["send", str(file), "--to", "joris@example.org", "--accept-terms"],
                         capsys)

    assert code == 0, err
    assert fake_uu.filesender_logins == 1 and through_the_browser.completed == [1]
    assert through_the_browser.uploaded(100) == b"hello world!"


@pytest.mark.browser
def test_one_code_covers_brightspace_and_filesender(fake_uu, through_the_browser, at_terminal,
                                                    monkeypatch, capsys):
    run(brightspace, ["login"], capsys)
    monkeypatch.setattr(auth, "interactive", lambda: False)

    code, _, err = run(filesender, ["login"], capsys)

    assert code == 0, err
    assert "Logged in with your existing UU session." in err
    assert len(fake_uu.codes) == 1


@pytest.mark.browser
def test_the_session_is_kept_for_the_next_run(fake_uu, through_the_browser, at_terminal,
                                              monkeypatch, capsys):
    run(filesender, ["login"], capsys)
    monkeypatch.setattr(auth, "interactive", lambda: False)

    code, out, err = run(filesender, ["transfers"], capsys)

    assert code == 0, err
    assert out == "No transfers.\n" and fake_uu.filesender_logins == 1
