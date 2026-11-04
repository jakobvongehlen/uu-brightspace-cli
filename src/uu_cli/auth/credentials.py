"""Solis-id, password and (optionally) TOTP secret, stored encrypted with a passphrase."""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from uu_cli import env, paths
from uu_cli.auth.totp_secret import totp

# scrypt(passphrase) -> AES-256-GCM over {"solisid","password","totp"}.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


def _creds_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=n, r=r, p=p,
                          maxmem=256 * 1024 * 1024, dklen=32)


def _passphrase(prompt: str = "Credentials passphrase: ") -> str:
    # UU_PASSPHRASE allows non-interactive runs (cron, Docker).
    return env.get("UU_PASSPHRASE") or getpass.getpass(prompt)


def save_creds(creds: dict, passphrase: str) -> None:
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _creds_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    ct = AESGCM(key).encrypt(nonce, json.dumps(creds).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()
    paths.write_private(paths.CREDS_FILE, json.dumps({
        "v": 1, "kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
        "salt": b64(salt), "nonce": b64(nonce), "ct": b64(ct),
    }))


def load_creds() -> dict:
    blob = json.loads(paths.CREDS_FILE.read_text())
    unb64 = base64.b64decode
    key = _creds_key(_passphrase(), unb64(blob["salt"]),
                     blob["n"], blob["r"], blob["p"])
    try:
        return json.loads(AESGCM(key).decrypt(unb64(blob["nonce"]),
                                              unb64(blob["ct"]), None))
    except InvalidTag:
        sys.exit(f"ERROR: wrong passphrase or corrupted {paths.CREDS_FILE}")


def have_creds(args) -> bool:
    """Fill missing login details from the encrypted credentials file, if any."""
    if not (args.solisid and args.password and args.totp) and paths.CREDS_FILE.exists():
        creds = load_creds()
        args.solisid  = args.solisid  or creds.get("solisid")
        args.password = args.password or creds.get("password", "")
        args.totp     = args.totp     or creds.get("totp", "")
    return bool(args.password and args.totp)


def cmd_save_creds(args) -> int:
    """Prompt for login details and store them encrypted in CREDS_FILE."""
    solisid  = args.solisid  or input("Solis-id: ").strip()
    password = args.password or getpass.getpass("Password: ")
    secret   = (args.totp or getpass.getpass(
        "TOTP secret (leave empty to type the code from your app at each login): ")
    ).replace(" ", "").upper()
    try:
        if secret:
            totp(secret)
    except Exception:
        print("ERROR: TOTP secret is not valid base32 (e.g. JBSWY3DPEHPK3PXP).",
              file=sys.stderr)
        return 1
    if not (solisid and password):
        print("ERROR: Solis-id and password are required.", file=sys.stderr)
        return 1
    passphrase = _passphrase("New passphrase: ")
    if not env.get("UU_PASSPHRASE"):
        if passphrase != getpass.getpass("Repeat passphrase: "):
            print("ERROR: Passphrases do not match.", file=sys.stderr)
            return 1
    if len(passphrase) < 8:
        print("ERROR: Use a passphrase of at least 8 characters.", file=sys.stderr)
        return 1
    save_creds({"solisid": solisid, "password": password, "totp": secret}, passphrase)
    print(f"Credentials encrypted to {paths.CREDS_FILE}", file=sys.stderr)
    return 0
