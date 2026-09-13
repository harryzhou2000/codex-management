"""Build a session catalog from Codex databases plus the rollout tree.

Codex models three kinds of threads:

- main agents (thread_source "user"): started by a person, can take user input;
- subagents (thread_source "subagent"): spawned by another thread, driven only by the parent;
- guardian reviews (thread_source "guardian_review"): automated review threads.

Parentage comes from thread_spawn_edges, whose child_thread_id is the primary key, so a subagent
has exactly one parent there. agent_path is a weaker second signal (a child path is its parent
path plus one segment) used as a cross-check: a disagreement, an ambiguous path match, or a
cycle is recorded as a parent_issue instead of being resolved silently.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from . import paths, util

CSV_COLUMNS = [
    "session_id", "display_name", "name_source", "name", "title", "thread_source",
    "role_label", "thread_depth", "parent_thread_id", "parent_source", "parent_issue",
    "root_thread_id", "subagent_count", "tree_size", "tree_bytes", "tree_size_human",
    "tree_latest_activity", "tree_recently_active", "agent_path", "agent_nickname",
    "agent_role", "archived", "tree", "exists", "orphan", "size_bytes", "size_human",
    "last_activity", "recently_active", "rollout_path", "project_path", "cwd", "created",
    "updated", "turns", "tokens_used", "cli_version", "git_branch", "model",
    "first_user_message",
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

# The symbolic parent of a top-level subagent path; the main agent carries no agent_path.
ROOT_PATH = "/root"


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
    "cli_version", "git_branch", "model", "source", "model_provider", "thread_source",
    "agent_path", "agent_nickname", "agent_role",
]


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(paths.sqlite_ro_uri(path), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?", (table,)
    ).fetchone()
    return row is not None


def fetch_threads(db: Path) -> list[dict]:
    with _connect(db) as connection:
        # Column sets differ between Codex releases; select only what this database has.
        available = {row["name"] for row in connection.execute("pragma table_info(threads)")}
        columns = [column for column in THREAD_COLUMNS if column in available]
        return [dict(row) for row in connection.execute(f"select {', '.join(columns)} from threads")]


def fetch_spawn_edges(db: Path) -> dict[str, str]:
    """child thread id -> parent thread id. child_thread_id is the table's primary key."""

    try:
        with _connect(db) as connection:
            if not _table_exists(connection, "thread_spawn_edges"):
                return {}
            return {
                row["child_thread_id"]: row["parent_thread_id"]
                for row in connection.execute(
                    "select parent_thread_id, child_thread_id from thread_spawn_edges"
                )
            }
    except sqlite3.Error:
        return {}


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


def resolve_parent(
    session_id: str, agent_path: str, edges: dict[str, str], path_index: dict[str, list[str]]
) -> tuple[str | None, str, str]:
    """Return (parent_id, parent_source, issue) using the edge table first, then agent_path."""

    edge_parent = edges.get(session_id)
    path_parent: str | None = None
    path_issue = ""
    path = (agent_path or "").strip()
    if path and "/" in path:
        parent_path = path.rsplit("/", 1)[0]
        if parent_path and parent_path != ROOT_PATH:
            candidates = path_index.get(parent_path, [])
            if len(candidates) == 1:
                path_parent = candidates[0]
            elif len(candidates) > 1:
                path_issue = "ambiguous-parent-path"
            else:
                path_issue = "unresolved-parent-path"

    if edge_parent and path_parent and edge_parent != path_parent:
        return edge_parent, "edge", "parent-conflict"
    if edge_parent:
        return edge_parent, "edge", ""
    if path_parent:
        return path_parent, "path", ""
    return None, "", path_issue


def walk_ancestry(start: str, parent_of: dict[str, str]) -> tuple[list[str], bool]:
    """Chain from start to its root, plus a cycle flag."""

    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = start
    while current is not None:
        if current in seen:
            return chain, True
        seen.add(current)
        chain.append(current)
        current = parent_of.get(current)
    return chain, False


def build_tree(
    ids: list[str],
    parent_of: dict[str, str],
    issues: dict[str, str],
    sizes: dict[str, int],
    activity: dict[str, float],
) -> tuple[dict[str, str], dict[str, int], dict[str, int], dict[str, int], dict[str, float], dict[str, str]]:
    """Resolve roots, depths, descendant counts, tree bytes and newest member activity.

    Cycles are flagged and detached. Tree totals cover the whole subtree, which is what a
    tree-level criterion and the recently-active guard need.
    """

    cyclic: set[str] = set()
    for session_id in ids:
        if session_id in parent_of:
            chain, is_cycle = walk_ancestry(session_id, parent_of)
            if is_cycle:
                cyclic.update(chain)
    for session_id in cyclic:
        parent_of.pop(session_id, None)
        issues[session_id] = "cycle"

    roots: dict[str, str] = {}
    depths: dict[str, int] = {}
    for session_id in ids:
        if session_id in cyclic:
            continue
        chain, _ = walk_ancestry(session_id, parent_of)
        roots[session_id] = chain[-1]
        depths[session_id] = len(chain) - 1

    children: dict[str, list[str]] = {}
    for child, parent in parent_of.items():
        if child in cyclic:
            continue
        children.setdefault(parent, []).append(child)

    counts: dict[str, int] = {}
    totals: dict[str, int] = {}
    latest: dict[str, float] = {}
    for session_id in ids:
        own_size = sizes.get(session_id, 0)
        own_activity = activity.get(session_id, 0.0)
        if session_id in cyclic:
            counts[session_id] = 0
            totals[session_id] = own_size
            latest[session_id] = own_activity
            continue
        descendants = 0
        total_bytes = own_size
        newest = own_activity
        stack = list(children.get(session_id, []))
        while stack:
            child = stack.pop()
            descendants += 1
            total_bytes += sizes.get(child, 0)
            newest = max(newest, activity.get(child, 0.0))
            stack.extend(children.get(child, []))
        counts[session_id] = descendants
        totals[session_id] = total_bytes
        latest[session_id] = newest
    return roots, depths, counts, totals, latest, issues


def role_label(thread_source: str, depth: int | None) -> str:
    if thread_source == "user":
        return "main"
    if thread_source == "subagent":
        return f"sub d{depth}" if depth is not None else "sub"
    return thread_source or "unknown"


def build_rows(home: Path, recent_minutes: int = 15, scan_bytes: int = 1024 * 1024) -> list[dict]:
    state = paths.state_db(home)
    if state is None or not state.exists():
        raise SystemExit(f"no state_*.sqlite under {home}")

    threads = fetch_threads(state)
    turns = fetch_turn_counts(paths.thread_history_db(home))
    edges = fetch_spawn_edges(state)
    index = index_rollouts(home)
    now = util.dt.datetime.now().timestamp()

    by_id = {thread.get("id") or "": thread for thread in threads}
    path_index: dict[str, list[str]] = {}
    for session_id, thread in by_id.items():
        path = (thread.get("agent_path") or "").strip()
        if path:
            path_index.setdefault(path, []).append(session_id)

    parent_of: dict[str, str] = {}
    parent_source: dict[str, str] = {}
    issues: dict[str, str] = {}
    for session_id, thread in by_id.items():
        parent, source, issue = resolve_parent(
            session_id, thread.get("agent_path") or "", edges, path_index
        )
        if parent:
            parent_of[session_id] = parent
            parent_source[session_id] = source
        if issue:
            issues[session_id] = issue

    resolved_path: dict[str, Path | None] = {}
    sizes: dict[str, int] = {}
    activity: dict[str, float] = {}
    for session_id, thread in by_id.items():
        recorded_path = thread.get("rollout_path") or ""
        recorded = Path(recorded_path) if recorded_path else None
        path = index.get(session_id) or (recorded if recorded and recorded.exists() else None)
        resolved_path[session_id] = path
        sizes[session_id] = util.size_or_zero(path) if path else 0
        mtime = util.mtime_or_zero(path) if path else 0.0
        updated_at_ms = thread.get("updated_at_ms") or 0
        activity[session_id] = mtime or (
            float(updated_at_ms) / 1000.0 if updated_at_ms else float(thread.get("updated_at") or 0)
        )

    roots, depths, counts, totals, latest, issues = build_tree(
        list(by_id), parent_of, issues, sizes, activity
    )

    rows: list[dict] = []
    for session_id, thread in by_id.items():
        recorded_path = thread.get("rollout_path") or ""
        path = resolved_path.get(session_id)
        size = sizes.get(session_id, 0)
        last_activity = activity.get(session_id, 0.0)
        updated_at_ms = thread.get("updated_at_ms") or 0
        cwd = thread.get("cwd") or ""
        thread_source = thread.get("thread_source") or ""
        depth = depths.get(session_id)
        display_name, name_source = resolve_display_name(thread, path, scan_bytes)
        rows.append({
            "session_id": session_id,
            "display_name": display_name,
            "name_source": name_source,
            "name": (thread.get("name") or "").strip()[:120],
            "title": (thread.get("title") or "").strip()[:120],
            "thread_source": thread_source,
            "role_label": role_label(thread_source, depth),
            "thread_depth": depth if depth is not None else -1,
            "parent_thread_id": parent_of.get(session_id, ""),
            "parent_source": parent_source.get(session_id, ""),
            "parent_issue": issues.get(session_id, ""),
            "root_thread_id": roots.get(session_id, session_id),
            "subagent_count": counts.get(session_id, 0),
            "tree_size": counts.get(session_id, 0) + 1,
            "tree_bytes": totals.get(session_id, size),
            "tree_size_human": util.human_size(totals.get(session_id, size)),
            "tree_latest_activity": util.local_iso(latest.get(session_id, 0.0)),
            "tree_recently_active": bool(
                latest.get(session_id) and (now - latest[session_id]) < recent_minutes * 60
            ),
            "agent_path": (thread.get("agent_path") or "").strip(),
            "agent_nickname": thread.get("agent_nickname") or "",
            "agent_role": thread.get("agent_role") or "",
            "archived": int(thread.get("archived") or 0),
            "tree": classify(path, home),
            "exists": bool(path),
            "orphan": False,
            "size_bytes": size,
            "size_human": util.human_size(size),
            "last_activity": util.local_iso(last_activity),
            "last_activity_epoch": last_activity,
            "recently_active": bool(last_activity and (now - last_activity) < recent_minutes * 60),
            "rollout_path": str(path) if path else recorded_path,
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

    known = set(by_id)
    for session_id, path in sorted(index.items()):
        if session_id in known:
            continue
        size = util.size_or_zero(path)
        mtime = util.mtime_or_zero(path)
        rows.append({
            "session_id": session_id,
            "display_name": "",
            "name_source": "none",
            "name": "", "title": "(no state row)",
            "thread_source": "orphan",
            "role_label": "orphan",
            "thread_depth": 0,
            "parent_thread_id": "", "parent_source": "", "parent_issue": "",
            "root_thread_id": session_id, "subagent_count": 0, "tree_size": 1,
            "tree_bytes": size, "tree_size_human": util.human_size(size),
            "tree_latest_activity": util.local_iso(mtime),
            "tree_recently_active": bool(mtime and (now - mtime) < recent_minutes * 60),
            "agent_path": "", "agent_nickname": "", "agent_role": "",
            "archived": 1 if path.parent == paths.archived_dir(home) else 0,
            "tree": classify(path, home),
            "exists": True,
            "orphan": True,
            "size_bytes": size,
            "size_human": util.human_size(size),
            "last_activity": util.local_iso(mtime),
            "last_activity_epoch": mtime,
            "recently_active": bool(mtime and (now - mtime) < recent_minutes * 60),
            "rollout_path": str(path),
            "project_path": "", "project": "", "cwd": "",
            "created": util.local_iso(mtime), "updated": util.local_iso(mtime),
            "turns": 0, "tokens_used": 0, "cli_version": "", "git_branch": "",
            "model": "", "first_user_message": "",
        })
    return rows


def tree_stats(rows: list[dict]) -> dict[str, int]:
    stats: dict[str, int] = {}
    for row in rows:
        key = row.get("thread_source") or "unknown"
        stats[key] = stats.get(key, 0) + 1
    stats["issues"] = sum(1 for row in rows if row.get("parent_issue"))
    return stats


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
