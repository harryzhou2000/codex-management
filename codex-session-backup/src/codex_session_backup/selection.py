"""Pick backup/delete candidates out of a catalog."""

from __future__ import annotations

from typing import Iterable

from . import util


def apply_filters(
    rows: Iterable[dict],
    *,
    older_than: str | None = None,
    min_size: str | None = None,
    include_archived: bool = True,
    include_orphans: bool = True,
    skip_recently_active: bool = True,
    min_turns: int | None = None,
) -> list[dict]:
    """Criteria are ANDed. The date criterion means last activity strictly before that day."""

    cutoff = util.parse_date(older_than).timestamp() if older_than else None
    floor = util.parse_size(min_size) if min_size else None
    selected: list[dict] = []

    for row in rows:
        reasons: list[str] = []
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
            reasons.append(f"last activity before {older_than}")
        if floor is not None:
            if row.get("size_bytes", 0) <= floor:
                continue
            reasons.append(f"size > {min_size}")
        if min_turns is not None and row.get("turns", 0) < min_turns:
            continue
        enriched = dict(row)
        enriched["reason"] = "; ".join(reasons)
        selected.append(enriched)

    selected.sort(key=lambda item: item.get("size_bytes", 0), reverse=True)
    return selected


def summarize(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    total = sum(row.get("size_bytes", 0) for row in rows)
    archived = sum(1 for row in rows if row.get("archived"))
    return {
        "sessions": len(rows),
        "total_bytes": total,
        "total_human": util.human_size(total),
        "archived_sessions": archived,
    }
