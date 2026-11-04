"""Environment variables. They start with UU_; the names from before (BRIGHTSPACE_*, from before
every service shared the login, and KONJOIN_*) still work."""

from __future__ import annotations

import os

# Each variable's name from before, where it had one.
OLD_NAMES = {
    "UU_SOLISID": "BRIGHTSPACE_SOLISID",
    "UU_PASSWORD": "BRIGHTSPACE_PASSWORD",
    "UU_TOTP_SECRET": "BRIGHTSPACE_TOTP_SECRET",
    "UU_PASSPHRASE": "BRIGHTSPACE_PASSPHRASE",
    "UU_AUTH": "BRIGHTSPACE_AUTH",
    "UU_SESSION_DIR": "BRIGHTSPACE_SESSION_DIR",
    "UU_CREDS_FILE": "BRIGHTSPACE_CREDS_FILE",
    "UU_BRIGHTSPACE_TOKEN_FILE": "BRIGHTSPACE_TOKEN_FILE",
    "UU_BRIGHTSPACE_URL": "BRIGHTSPACE_BASE_URL",
    "UU_BRIGHTSPACE_ORG_UNIT": "BRIGHTSPACE_ORG_UNIT",
    "UU_KONJOIN_URL": "KONJOIN_BASE_URL",
    "UU_KONJOIN_IDENTITY_PROVIDER": "KONJOIN_IDENTITY_PROVIDER",
}


def get(name: str, default: str | None = None) -> str | None:
    """$name, else the variable under its old name, else default."""
    old = OLD_NAMES.get(name)
    return os.environ.get(name) or (old and os.environ.get(old)) or default
