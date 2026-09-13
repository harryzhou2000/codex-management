"""Build a session catalog from Codex databases plus the rollout tree."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from . import paths, util

CSV_COLUMNS = [
    "session_id", "display_name", "name_source", "name", "title", "archived", "tree", "exists",
    "orphan", "size_bytes", "size_human", "last_activity", "recently_active", "rollout_path",
    "project_path", "cwd", "created", "updated", "turns", "tokens_used", "cli_version",
    "git_branch", "model", "first_user_message",
]

# Text the agent injects into the transcript; never a user-authored session headline.
INJECTED_PREFIXES = (
    "# AGENTS.md instructions", "# AGENTS.md", "<permissions instructions>",
    "<environment_context>", "<skills_instructions>", "<apps_instructions>",
    "<codex_internal_context", "<user_instructions", "<turn_context", "<system",
    "You are ", "<INSTRUCTIONS",
)

# Nudges that carry no topic.
TRIVIAL_MESSAGES = {
    ".", "..", "...", "?", "??", "ok", "okay", "yes", "no", "y", "n", "continue",
    "go", "go on", "next", "please continue", "keep going", "more",
}

MIN_HEADLINE_CHARS = 12


def meaningful(text: str | None, min_chars: int = 8) -> bool:
    value = " ".join((text or "").split())
    if len(value) < min_chars:
        return False
    return value.lower().rstrip(" .") not in TRIVIAL_MESSAGES


def _user_text(record: dict) -> str:
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("role") != "user":
        return ""
    if isinstance(payload.get("message"), str):
        return payload["message"]
    content = payload.get("content")
    if isinstance(content, list):
        return " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    return ""


def headline_from_rollout(path: Path, max_bytes: int) -> str:
    """First substantial user message in the head of a rollout, for sessions with no real title."""

    if max_bytes <= 0:
        return ""
    try:
        with path.open("rb") as handle:
            head = handle.read(max_bytes)
    except OSError:
        return ""
    for line in head.split(b"\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        text = " ".join(_user_text(record).split())
        if len(text) < MIN_HEADLINE_CHARS or text.startswith(INJECTED_PREFIXES):
            continue
        if not meaningful(text, MIN_HEADLINE_CHARS):
            continue
        return text[:160]
    return ""


def pretty_path(value: str) -> str:
    home = str(Path.home())
    return "~" + value[len(home):] if value.startswith(home) else value


def resolve_display_name(thread: dict, path: Path | None, scan_bytes: int) -> tuple[str, str]:
    """A user-set name wins, then the generated title, then the stored first message,
    then the first substantial prompt found in the head of the rollout."""

    name = (thread.get("name") or "").strip()
    if name:
        return " ".join(name.split())[:160], "name"
    for key, source in (("title", "title"), ("first_user_message", "first_message")):
        value = thread.get(key)
        if meaningful(value, 8):
            return " ".join((value or "").split())[:160], source
    if path is not None:
        headline = headline_from_rollout(path, scan_bytes)
        if headline:
            return headline, "rollout_head"
    return "", "none"

THREAD_COLUMNS = [
    "id", "name", "title", "first_user_message", "cwd", "rollout_path", "created_at",
    "updated_at", "created_at_ms", "updated_at_ms", "archived", "archived_at", "tokens_used",
    "cli_version", "git_branch", "model", "source", "model_provider",
]


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(paths.sqlite_ro_uri(path), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def fetch_threads(db: Path) -> list[dict]:
    with _connect(db) as connection:
        # Column sets differ between Codex releases; select only what this database has.
        available = {row["name"] for row in connection.execute("pragma table_info(threads)")}
        columns = [column for column in THREAD_COLUMNS if column in available]
        return [dict(row) for row in connection.execute(f"select {', '.join(columns)} from threads")]


def fetch_turn_counts(db: Path | None) -> dict[str, int]:
    if not db or not db.exists():
        return {}
    try:
        with _connect(db) as connection:
            rows = connection.execute(
                "select thread_id, count(*) as turns from thread_turns group by thread_id"
            )
            return {row["thread_id"]: row["turns"] for row in rows}
    except sqlite3.Error:
        return {}


def index_rollouts(home: Path) -> dict[str, Path]:
    """Map session id -> rollout file for both the live and archived trees."""

    index: dict[str, Path] = {}
    for root in (paths.sessions_dir(home), paths.archived_dir(home)):
        if not root.exists():
            continue
        for file in root.rglob("rollout-*.jsonl"):
            match = paths.ROLLOUT_RE.match(file.name)
            if match:
                index[match.group(1)] = file
    return index


def classify(path: Path | None, home: Path) -> str:
    if path is None:
        return "missing"
    try:
        relative = path.relative_to(home)
    except ValueError:
        return "external"
    return relative.parts[0] if relative.parts else "external"


def build_rows(home: Path, recent_minutes: int = 15, scan_bytes: int = 1024 * 1024) -> list[dict]:
    state = paths.state_db(home)
    if state is None or not state.exists():
        raise SystemExit(f"no state_*.sqlite under {home}")

    threads = fetch_threads(state)
    turns = fetch_turn_counts(paths.thread_history_db(home))
    index = index_rollouts(home)
    now = util.dt.datetime.now().timestamp()
    rows: list[dict] = []

    for thread in threads:
        session_id = thread.get("id") or ""
        recorded_path = thread.get("rollout_path") or ""
        recorded = Path(recorded_path) if recorded_path else None
        path = index.get(session_id) or (recorded if recorded and recorded.exists() else None)
        size = util.size_or_zero(path) if path else 0
        mtime = util.mtime_or_zero(path) if path else 0.0
        updated_at_ms = thread.get("updated_at_ms") or 0
        last_activity = mtime or (
            float(updated_at_ms) / 1000.0 if updated_at_ms else float(thread.get("updated_at") or 0)
        )
        cwd = thread.get("cwd") or ""
        display_name, name_source = resolve_display_name(thread, path, scan_bytes)
        rows.append({
            "session_id": session_id,
            "display_name": display_name,
            "name_source": name_source,
            "name": (thread.get("name") or "").strip()[:120],
            "tree": classify(path, home),
            "exists": bool(path),
            "orphan": False,
            "archived": int(thread.get("archived") or 0),
            "size_bytes": size,
            "size_human": util.human_size(size),
            "last_activity": util.local_iso(last_activity),
            "last_activity_epoch": last_activity,
            "recently_active": bool(last_activity and (now - last_activity) < recent_minutes * 60),
            "rollout_path": str(path) if path else recorded_path,
            "title": (thread.get("title") or "").strip()[:120],
            "project_path": pretty_path(cwd),
            "project": Path(cwd).name if cwd else "",
            "cwd": cwd,
            "created": util.local_iso(thread.get("created_at")),
            "updated": util.local_iso_ms(updated_at_ms) or util.local_iso(thread.get("updated_at")),
            "turns": turns.get(session_id, 0),
            "tokens_used": thread.get("tokens_used") or 0,
            "cli_version": thread.get("cli_version") or "",
            "git_branch": thread.get("git_branch") or "",
            "model": thread.get("model") or "",
            "first_user_message": (thread.get("first_user_message") or "").strip()[:160],
        })

    known = {thread.get("id") for thread in threads}
    for session_id, path in sorted(index.items()):
        if session_id in known:
            continue
        size = util.size_or_zero(path)
        mtime = util.mtime_or_zero(path)
        rows.append({
            "session_id": session_id,
            "display_name": "",
            "name_source": "none",
            "name": "",
            "tree": classify(path, home),
            "exists": True,
            "orphan": True,
            "archived": 1 if path.parent == paths.archived_dir(home) else 0,
            "size_bytes": size,
            "size_human": util.human_size(size),
            "last_activity": util.local_iso(mtime),
            "last_activity_epoch": mtime,
            "recently_active": bool(mtime and (now - mtime) < recent_minutes * 60),
            "rollout_path": str(path),
            "title": "(no state row)",
            "project_path": "",
            "project": "", "cwd": "",
            "created": util.local_iso(mtime), "updated": util.local_iso(mtime),
            "turns": 0, "tokens_used": 0, "cli_version": "", "git_branch": "",
            "model": "", "first_user_message": "",
        })
    return rows


def write_outputs(rows: list[dict], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "catalog.csv"
    json_path = out_dir / "catalog.json"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return csv_path, json_path
