"""Where the CLI keeps its state: browser session, token, saved credentials and downloads."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from uu_cli import env

# A source checkout keeps its state next to the repo, as brightspace-cli.py always did. An
# installed package has no checkout, so it falls back to the user data dir.
_CHECKOUT = Path(__file__).parents[2]
if (_CHECKOUT / "pyproject.toml").exists():
    STATE_PARENT = _CHECKOUT.parent
else:
    STATE_PARENT = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "uu-cli"

# Before every service shared it, the state was Brightspace's: ensure_state_dirs moves it over.
OLD_STATE_DIR = STATE_PARENT / "brightspace"
DEFAULT_STATE_DIR = STATE_PARENT / "uu-state"

# Setups from before UU_STATE_DIR named the token file instead; everything else lived next to it.
_OLD_TOKEN_FILE = os.environ.get("BRIGHTSPACE_TOKEN_FILE")
STATE_DIR = Path(os.environ.get("UU_STATE_DIR")
                 or (Path(_OLD_TOKEN_FILE).parent if _OLD_TOKEN_FILE else DEFAULT_STATE_DIR))

SESSION_DIR = Path(env.get("UU_SESSION_DIR") or STATE_DIR / "session")
TOKEN_FILE = Path(env.get("UU_BRIGHTSPACE_TOKEN_FILE") or STATE_DIR / "token.json")
CREDS_FILE = Path(env.get("UU_CREDS_FILE") or STATE_DIR / "credentials.enc")
OUTPUT_DIR = STATE_PARENT / "brightspace_sync"


def ensure_state_dirs() -> None:
    """Create the state directories. Session cookies, the token and debug dumps are bearer secrets: owner-only."""
    move_old_state()
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    SESSION_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    SESSION_DIR.chmod(0o700)
    TOKEN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        TOKEN_FILE.chmod(0o600)


def move_old_state() -> None:
    """Move the state from OLD_STATE_DIR to the default STATE_DIR, once, unless UU_STATE_DIR says otherwise."""
    if STATE_DIR != DEFAULT_STATE_DIR or STATE_DIR.exists() or not OLD_STATE_DIR.is_dir():
        return
    OLD_STATE_DIR.rename(STATE_DIR)
    print(f"[UU] Moved the login session and saved credentials from {OLD_STATE_DIR} to {STATE_DIR}.",
          file=sys.stderr)


def write_private(path: Path, data: str) -> None:
    """Write a file readable only by the owner (also tightens an existing file)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(data)
    path.chmod(0o600)


def safe_filename(s: str | Path) -> str:
    """Sanitize a string for use as a filename — removes filesystem-unsafe chars."""
    return str(s or "untitled").replace("\\", "_").replace("\n", " ").strip()[:180]
