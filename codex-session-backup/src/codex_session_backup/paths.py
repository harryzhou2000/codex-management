"""Locate Codex data files without ever writing to them."""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_ARCHIVE_ROOT = "~/ssd1/bkp/codex_sessions"
ROLLOUT_RE = re.compile(
    r"^rollout-.*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def codex_home(value: str | os.PathLike[str] | None = None) -> Path:
    """CODEX_HOME if set, else ~/.codex, unless an explicit path is given."""

    if value:
        return Path(value).expanduser()
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _highest_versioned(home: Path, pattern: str) -> Path | None:
    """Pick state_<n>.sqlite style files by their numeric suffix, highest wins."""

    def version(path: Path) -> int:
        match = re.search(r"_(\d+)\.sqlite$", path.name)
        return int(match.group(1)) if match else -1

    candidates = sorted(home.glob(pattern), key=version)
    return candidates[-1] if candidates else None


def state_db(home: Path) -> Path | None:
    return _highest_versioned(home, "state_*.sqlite")


def thread_history_db(home: Path) -> Path | None:
    return _highest_versioned(home, "thread_history_*.sqlite")


def sessions_dir(home: Path) -> Path:
    return home / "sessions"


def archived_dir(home: Path) -> Path:
    return home / "archived_sessions"


def sqlite_ro_uri(path: Path) -> str:
    """Read-only URI so a catalog run cannot mutate Codex state."""

    return f"file:{path}?mode=ro"
