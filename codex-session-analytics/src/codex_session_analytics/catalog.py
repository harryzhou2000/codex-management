"""Join Codex thread metadata, live rollouts, and the backup ledger."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import ledger_path, sqlite_ro_uri

INJECTED_PREFIXES = (
    "# AGENTS.md instructions",
    "# AGENTS.md",
    "<permissions instructions>",
    "<environment_context>",
    "<skills_instructions>",
    "<apps_instructions>",
    "<codex_internal_context",
    "<user_instructions",
    "<turn_context",
    "<system",
    "You are ",
    "<INSTRUCTIONS",
)

TRIVIAL_MESSAGES = {
    ".",
    "..",
    "...",
    "?",
    "??",
    "ok",
    "okay",
    "yes",
    "no",
    "y",
    "n",
    "continue",
    "go",
    "go on",
    "next",
    "please continue",
    "keep going",
    "more",
}


def meaningful(value: str | None, min_chars: int = 8) -> bool:
    text = " ".join((value or "").split())
    if len(text) < min_chars:
        return False
    if text.startswith(INJECTED_PREFIXES):
        return False
    return text.lower().rstrip(" .") not in TRIVIAL_MESSAGES


@dataclass
class ThreadMeta:
    session_id: str
    name: str = ""
    title: str = ""
    first_user_message: str = ""
    cwd: str = ""
    rollout_path: str = ""
    created_at: int = 0
    updated_at: int = 0
    model: str = ""
    tokens_used: int = 0
    thread_source: str = "unknown"
    archived: int = 0
    cli_version: str = ""
    agent_path: str = ""
    agent_nickname: str = ""


THREAD_COLUMNS = [
    "id",
    "name",
    "title",
    "first_user_message",
    "cwd",
    "rollout_path",
    "created_at",
    "updated_at",
    "model",
    "tokens_used",
    "thread_source",
    "archived",
    "cli_version",
    "agent_path",
    "agent_nickname",
]


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(sqlite_ro_uri(path), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_threads(db: Path | None) -> dict[str, ThreadMeta]:
    if db is None or not db.exists():
        return {}
    result: dict[str, ThreadMeta] = {}
    with _connect(db) as connection:
        available = {row["name"] for row in connection.execute("pragma table_info(threads)")}
        columns = [column for column in THREAD_COLUMNS if column in available]
        if "id" not in columns:
            return result
        for row in connection.execute(f"select {', '.join(columns)} from threads"):
            data = dict(row)
            session_id = str(data.get("id") or "")
            if not session_id:
                continue
            result[session_id] = ThreadMeta(
                session_id=session_id,
                name=str(data.get("name") or ""),
                title=str(data.get("title") or ""),
                first_user_message=str(data.get("first_user_message") or ""),
                cwd=str(data.get("cwd") or ""),
                rollout_path=str(data.get("rollout_path") or ""),
                created_at=int(data.get("created_at") or 0),
                updated_at=int(data.get("updated_at") or 0),
                model=str(data.get("model") or ""),
                tokens_used=int(data.get("tokens_used") or 0),
                thread_source=str(data.get("thread_source") or "unknown"),
                archived=int(data.get("archived") or 0),
                cli_version=str(data.get("cli_version") or ""),
                agent_path=str(data.get("agent_path") or ""),
                agent_nickname=str(data.get("agent_nickname") or ""),
            )
    return result


def _parse_activity(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def load_ledger(archive_root: Path | None) -> list[dict]:
    if archive_root is None:
        return []
    path = ledger_path(archive_root)
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not record.get("session_id") or not record.get("archive_path"):
                continue
            record["last_activity_epoch"] = _parse_activity(record.get("last_activity"))
            records.append(record)
    return records


def ledger_index(records: list[dict]) -> dict[str, dict]:
    selected: dict[str, dict] = {}
    for record in records:
        session_id = str(record.get("session_id") or "")
        if not session_id:
            continue
        previous = selected.get(session_id)
        if previous is None or str(record.get("backed_up_at") or "") > str(
            previous.get("backed_up_at") or ""
        ):
            selected[session_id] = record
    return selected


def display_name(meta: ThreadMeta | None, ledger: dict | None) -> tuple[str, str]:
    if meta is not None:
        if meta.name.strip():
            return " ".join(meta.name.split())[:160], "name"
        if meaningful(meta.title, 3):
            return " ".join(meta.title.split())[:160], "title"
        if meaningful(meta.first_user_message, 12):
            return " ".join(meta.first_user_message.split())[:160], "first_message"
    if ledger is not None and meaningful(str(ledger.get("title") or ""), 3):
        return " ".join(str(ledger.get("title") or "").split())[:160], "ledger_title"
    return "(untitled)", "none"


def pretty_path(value: str) -> str:
    home = str(Path.home())
    return "~" + value[len(home) :] if value.startswith(home) else value
