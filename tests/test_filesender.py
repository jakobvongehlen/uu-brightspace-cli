"""SURFfilesender: the signed REST calls against a fake server, and the login in a real Chromium."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import stat
import time
from urllib.parse import urlparse

import pyotp
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fake_uu import FILESENDER_SECRET, FILESENDER_USER, PASSWORD, SOLISID, TOTP_SECRET
from uu_cli import auth
from uu_cli.services.brightspace.cli import main as brightspace
from uu_cli.services.filesender import api
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
    """The REST API, checking each signature the way FileSender's AuthRemote.class.php does."""

    def __init__(self):
        self.user, self.secret = "uid-42", "s3cret"
        self.chunk = 4
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        self.transfers: dict[int, dict] = {}
        self.chunks: dict[int, dict[int, bytes]] = {}
        self.files_done: list[int] = []
        self.completed: list[int] = []
        self.deleted: list[int] = []
        self.fail_chunk: tuple[int, int] | None = None

    def __call__(self, method, url, headers, body):
        u = urlparse(url)
        if u.path == "/rest.php/info":
            return 200, json.dumps({"upload_chunk_size": self.chunk}).encode()
        if u.path == "/filesender-config.js.php":
            return 200, CONFIG_JS.encode()
        path = u.path.removeprefix("/rest.php")
        params = dict(p.split("=", 1) for p in u.query.split("&"))
        received = params.pop("signature")
        signed = f"{method.lower()}&{u.netloc}/rest.php{path}?" + "&".join(sorted(f"{k}={v}" for k, v in params.items()))
        signed = signed.encode() + (b"&" + body if body else b"")
        if params.get("remote_user") != self.user or received != hmac.new(
                self.secret.encode(), signed, hashlib.sha1).hexdigest():
            return 403, json.dumps({"message": "auth_remote_signature_check_failed"}).encode()
        if time.time() - int(params["timestamp"]) - 15 > 0:
            return 403, json.dumps({"message": "auth_remote_too_late"}).encode()
        self.requests.append((method, path, params, body))
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
    fake = FakeFilesender()
    monkeypatch.setattr(api, "_send", fake)
    monkeypatch.setenv("UU_FILESENDER_USER", fake.user)
    monkeypatch.setenv("UU_FILESENDER_API_SECRET", fake.secret)
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


def test_send_signs_every_request_and_uploads_in_chunks(fs, file, capsys):
    code, out, err = run(filesender, ["send", str(file), "--to", "joris@example.org", "--accept-terms"], capsys)

    assert code == 0, err
    assert [(m, p) for m, p, _, _ in fs.requests] == [
        ("POST", "/transfer"), ("PUT", "/file/100/chunk/0"), ("PUT", "/file/100/chunk/4"),
        ("PUT", "/file/100/chunk/8"), ("PUT", "/file/100"), ("PUT", "/transfer/1")]
    assert fs.uploaded(100) == b"hello world!"
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


def test_a_wrong_secret_says_what_to_do(fs, file, monkeypatch, capsys):
    monkeypatch.setenv("UU_FILESENDER_API_SECRET", "an-old-secret")

    code, _, err = run(filesender, ["send", str(file), "--to", "joris@example.org", "--accept-terms"], capsys)

    assert code == 1
    assert "API secret doesn't match" in err and "uu filesender login" in err


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


def test_send_before_login_says_how(file, monkeypatch, capsys):
    monkeypatch.delenv("UU_FILESENDER_USER", raising=False)
    monkeypatch.delenv("UU_FILESENDER_API_SECRET", raising=False)

    code, _, err = run(filesender, ["send", str(file), "--to", "a@example.org", "--accept-terms"], capsys)

    assert code == 1 and "uu filesender login" in err


def test_the_signature_matches_filesenders_own_client():
    """The same bytes filesender.py signs: method&host/rest.php/path?sorted query&body."""
    params = {"remote_user": "uid-42", "timestamp": "1700000000", "key": "puid-1-0"}
    expected = hmac.new(b"s3cret", b"put&filesender.surf.nl/rest.php/file/100?key=puid-1-0"
                        b"&remote_user=uid-42&timestamp=1700000000&" + b'{"complete":true}',
                        hashlib.sha1).hexdigest()

    assert api.signature("s3cret", "put", "/file/100", params, b'{"complete":true}') == expected


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


@pytest.mark.browser
def test_login_keeps_the_api_details_owner_only(fake_uu, at_terminal, capsys):
    code, _, err = run(filesender, ["login"], capsys)

    assert code == 0, err
    assert "SURFconext: choosing Utrecht University" in err
    saved = api.account_file()
    assert json.loads(saved.read_text()) == {"username": FILESENDER_USER, "secret": FILESENDER_SECRET}
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert api.load_account() == api.Account(FILESENDER_USER, FILESENDER_SECRET)
    assert fake_uu.filesender_logins == 1
    assert fake_uu.filesender_terms and "Accepting \"SURFfilesender gebruiksvoorwaarden\"" in err


@pytest.mark.browser
def test_one_code_covers_brightspace_and_filesender(fake_uu, at_terminal, monkeypatch, capsys):
    run(brightspace, ["login"], capsys)
    monkeypatch.setattr(auth, "interactive", lambda: False)

    code, _, err = run(filesender, ["login"], capsys)

    assert code == 0, err
    assert "Logged in with your existing UU session." in err
    assert len(fake_uu.codes) == 1


@pytest.mark.browser
def test_login_without_an_api_secret_says_where_to_make_one(fake_uu, at_terminal, capsys):
    fake_uu.filesender_secret = ""

    code, _, err = run(filesender, ["login"], capsys)

    assert code == 1
    assert "no API secret" in err and "?s=user" in err
    assert not api.account_file().exists()
