"""Pick backup/delete candidates out of a catalog, optionally with their subagent trees.

Only main agents (thread_source "user") are selected by default: subagents are driven by their
parent and never take user input, so archiving or deleting one on its own is almost always a
mistake. When a main agent is selected with subagents="tree", its whole descendant set joins the
plan so a backup or deletion covers the session as a unit.

Tree resolution is strict about uniqueness. A child is attached only when its parent is known
and unambiguous: parent_issue rows (conflicting edge vs agent_path, ambiguous path match,
unresolved path, cycle) are never attached, and their subtree stays out of the plan with a
warning, because a wrong attachment would silently move someone else's session.
"""

from __future__ import annotations

from typing import Iterable

from . import util

MAIN_SOURCE = "user"


def _reason(older_than: str | None, min_size: str | None) -> str:
    parts = []
    if older_than:
        parts.append(f"last activity before {older_than}")
    if min_size:
        parts.append(f"size > {min_size}")
    return "; ".join(parts)


def _children_index(catalog: list[dict]) -> tuple[dict[str, list[dict]], list[str]]:
    by_id = {row.get("session_id"): row for row in catalog}
    children: dict[str, list[dict]] = {}
    blocked: list[str] = []
    for row in catalog:
        parent = (row.get("parent_thread_id") or "").strip()
        if not parent:
            continue
        session_id = row.get("session_id") or ""
        if row.get("parent_issue") or parent not in by_id:
            blocked.append(session_id)
            continue
        children.setdefault(parent, []).append(row)
    return children, blocked


def apply_filters(
    rows: Iterable[dict],
    *,
    older_than: str | None = None,
    min_size: str | None = None,
    include_archived: bool = True,
    include_orphans: bool = False,
    skip_recently_active: bool = True,
    min_turns: int | None = None,
    thread_sources: tuple[str, ...] = (MAIN_SOURCE,),
    subagents: str = "tree",
) -> tuple[list[dict], list[str]]:
    """Criteria are ANDed; the date criterion means last activity strictly before that day."""

    cutoff = util.parse_date(older_than).timestamp() if older_than else None
    floor = util.parse_size(min_size) if min_size else None
    catalog = list(rows)
    children, blocked = _children_index(catalog)
    wanted = {source.lower() for source in thread_sources}

    selected: list[dict] = []
    for row in catalog:
        source = (row.get("thread_source") or "").lower()
        if "any" not in wanted and source not in wanted:
            continue
        if not row.get("exists"):
            continue
        if row.get("orphan") and not include_orphans:
            continue
        if row.get("archived") and not include_archived:
            continue
        if row.get("recently_active") and skip_recently_active:
            continue
        if cutoff is not None:
            if not row.get("last_activity_epoch") or row["last_activity_epoch"] >= cutoff:
                continue
        if floor is not None and row.get("size_bytes", 0) <= floor:
            continue
        if min_turns is not None and row.get("turns", 0) < min_turns:
            continue
        item = dict(row)
        item["selection"] = "criteria"
        item["tree_root"] = row.get("session_id")
        item["tree_depth"] = 0
        item["reason"] = _reason(older_than, min_size)
        selected.append(item)

    warnings: list[str] = []
    if subagents == "tree":
        taken = {row.get("session_id") for row in selected}
        for root in sorted(selected, key=lambda item: item.get("thread_depth", 0)):
            root_id = root.get("session_id")
            stack = [(child, 1) for child in children.get(root_id, [])]
            while stack:
                child, depth = stack.pop()
                child_id = child.get("session_id")
                if child_id in taken:
                    # Already attached to this or another selected root: never attach twice.
                    continue
                item = dict(child)
                item["selection"] = "subtree"
                item["tree_root"] = root_id
                item["tree_depth"] = depth
                item["role_label"] = f"sub d{depth}"
                item["reason"] = f"subagent of {root_id}"
                taken.add(child_id)
                selected.append(item)
                stack.extend((grand, depth + 1) for grand in children.get(child_id, []))
    elif blocked:
        warnings.append(
            f"{len(set(blocked))} subagent thread(s) have a parent issue and stay out of every tree"
        )

    if subagents == "tree" and blocked:
        listed = ", ".join(sorted(set(blocked))[:4])
        more = " ..." if len(set(blocked)) > 4 else ""
        warnings.append(
            f"{len(set(blocked))} subagent thread(s) excluded from tree resolution "
            f"(ambiguous or conflicting parent): {listed}{more}"
        )

    selected.sort(key=lambda item: item.get("size_bytes", 0), reverse=True)
    return selected, warnings


def summarize(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    total = sum(row.get("size_bytes", 0) for row in rows)
    return {
        "sessions": len(rows),
        "main_agents": sum(
            1 for row in rows
            if row.get("selection") == "criteria" and (row.get("thread_source") or "") == MAIN_SOURCE
        ),
        "subagents": sum(1 for row in rows if row.get("selection") == "subtree"),
        "other_selected": sum(
            1 for row in rows
            if row.get("selection") == "criteria" and (row.get("thread_source") or "") != MAIN_SOURCE
        ),
        "archived_sessions": sum(1 for row in rows if row.get("archived")),
        "total_bytes": total,
        "total_human": util.human_size(total),
    }
