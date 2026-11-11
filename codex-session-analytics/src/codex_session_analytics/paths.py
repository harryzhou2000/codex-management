"""Locate Codex data without writing to it."""

from __future__ import annotations

import os
import re
from pathlib import Path

UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def codex_home(value: str | os.PathLike[str] | None = None) -> Path:
    if value:
        return Path(value).expanduser()
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _highest_versioned(home: Path, pattern: str) -> Path | None:
    def version(path: Path) -> int:
        match = re.search(r"_(\d+)\.sqlite$", path.name)
        return int(match.group(1)) if match else -1

    candidates = sorted(home.glob(pattern), key=version)
    return candidates[-1] if candidates else None


def state_db(home: Path) -> Path | None:
    return _highest_versioned(home, "state_*.sqlite")


def sessions_dir(home: Path) -> Path:
    return home / "sessions"


def archived_dir(home: Path) -> Path:
    return home / "archived_sessions"


def sqlite_ro_uri(path: Path) -> str:
    return f"file:{path}?mode=ro"


def session_id_from_path(path: Path) -> str | None:
    matches = UUID_RE.findall(path.name)
    return matches[-1] if matches else None


def default_cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "codex-session-analytics" / "index.sqlite"


def default_archive_root() -> Path | None:
    env = os.environ.get("CODEX_SESSION_ARCHIVE")
    if env:
        return Path(env).expanduser()
    candidate = Path.home() / "ssd1" / "bkp" / "codex_sessions"
    return candidate if candidate.exists() else None


def ledger_path(archive_root: Path) -> Path:
    return archive_root / "ledger.jsonl"
