"""Build per-session rows and render report artifacts."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .catalog import display_name, load_ledger, load_threads, ledger_index, meaningful, pretty_path
from .index import AnalyticsIndex
from .paths import state_db
from .pricing import ModelMix, PriceBook
from .rollout import TokenUsage


@dataclass
class SessionRow:
    session_id: str
    name: str
    name_source: str
    thread_source: str
    model: str
    models: list[dict] = field(default_factory=list)
    cwd: str = ""
    project: str = ""
    source: str = "missing"
    disk_bytes: int = 0
    source_bytes: int = 0
    tokens: int = 0
    retained_tokens: int = 0
    estimated_tokens: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    cost_usd: float | None = None
    estimated_cost_usd: float = 0.0
    priced_tokens: int = 0
    unpriced_tokens: int = 0
    cost_basis: str = "none"
    calls: int = 0
    segment_count: int = 0
    updates: int = 0
    created: int = 0
    rollout_path: str = ""
    archive_path: str = ""
    cli_version: str = ""
    agent_path: str = ""
    agent_nickname: str = ""

    @property
    def updated(self) -> int:
        return self.updates

    def model_summary(self) -> str:
        if not self.models:
            return self.model or "?"
        if len(self.models) == 1:
            return str(self.models[0].get("model") or self.model or "?")
        return f"{self.models[0].get('model') or self.model} +{len(self.models) - 1}"

    def label(self) -> str:
        if self.thread_source == "subagent" and self.agent_nickname:
            return f"{self.agent_nickname} · {self.name}"
        return self.name

    def serialize(self) -> dict[str, Any]:
        data = asdict(self)
        data["updated"] = self.updated
        data["project"] = pretty_path(self.project)
        return data


def _row_for(session_id: str, rows: dict[str, SessionRow]) -> SessionRow:
    row = rows.get(session_id)
    if row is None:
        row = SessionRow(
            session_id=session_id,
            name="(untitled)",
            name_source="none",
            thread_source="unknown",
            model="",
        )
        rows[session_id] = row
    return row


def _epoch(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _segment_usage(segment: dict, prefix: str) -> TokenUsage:
    return TokenUsage(
        input=int(segment.get(f"{prefix}_input_tokens") or 0),
        cached=int(segment.get(f"{prefix}_cached_input_tokens") or 0),
        cache_write=int(segment.get(f"{prefix}_cache_write_input_tokens") or 0),
        output=int(segment.get(f"{prefix}_output_tokens") or 0),
        reasoning=int(segment.get(f"{prefix}_reasoning_output_tokens") or 0),
    )


def _difference(lifetime: TokenUsage, retained: TokenUsage) -> TokenUsage:
    result = TokenUsage(
        input=max(0, lifetime.input - retained.input),
        cached=max(0, lifetime.cached - retained.cached),
        cache_write=max(0, lifetime.cache_write - retained.cache_write),
        output=max(0, lifetime.output - retained.output),
        reasoning=max(0, lifetime.reasoning - retained.reasoning),
    )
    gap = max(0, lifetime.total - retained.total)
    if result.total != gap:
        result.input = max(0, result.input + gap - result.total)
    return result


def _aggregate_segments(
    segments: list[dict],
    pricebook: PriceBook,
    fallback_model: str,
    *,
    recompute_costs: bool = False,
) -> tuple[TokenUsage, TokenUsage, list[dict], float, float, int, int]:
    """Return lifetime, retained, model rows, cost, estimated cost, priced, unpriced."""

    lifetime_segment = max(segments, key=lambda segment: int(segment.get("lifetime_tokens") or 0))
    lifetime = TokenUsage()
    for segment in segments:
        lifetime.add(_segment_usage(segment, "lifetime"))
    retained = TokenUsage()
    retained_cost = 0.0
    retained_priced = 0
    retained_unpriced = 0
    models: dict[str, dict] = {}
    for segment in segments:
        retained.add(_segment_usage(segment, "retained"))
        if not recompute_costs:
            retained_cost += float(segment.get("retained_cost_usd") or 0.0)
            retained_priced += int(segment.get("retained_priced_tokens") or 0)
            retained_unpriced += int(segment.get("retained_unpriced_tokens") or 0)
        for model_row in segment.get("models") or []:
            name = str(model_row.get("model") or "unknown")
            entry = models.setdefault(
                name,
                {
                    "model": name,
                    "calls": 0,
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "cost_usd": 0.0,
                    "priced_tokens": 0,
                    "unpriced_tokens": 0,
                    "estimated_tokens": 0,
                },
            )
            entry["calls"] += int(model_row.get("calls") or 0)
            entry["total_tokens"] += int(model_row.get("retained_tokens") or 0)
            entry["input_tokens"] += int(model_row.get("retained_input_tokens") or 0)
            entry["cached_input_tokens"] += int(model_row.get("retained_cached_input_tokens") or 0)
            entry["cache_write_input_tokens"] += int(
                model_row.get("retained_cache_write_input_tokens") or 0
            )
            entry["output_tokens"] += int(model_row.get("retained_output_tokens") or 0)
            entry["reasoning_output_tokens"] += int(
                model_row.get("retained_reasoning_output_tokens") or 0
            )
            if recompute_costs and pricebook.has_long_context(name):
                model_usage = TokenUsage(
                    input=int(model_row.get("retained_input_tokens") or 0),
                    cached=int(model_row.get("retained_cached_input_tokens") or 0),
                    cache_write=int(model_row.get("retained_cache_write_input_tokens") or 0),
                    output=int(model_row.get("retained_output_tokens") or 0),
                    reasoning=int(model_row.get("retained_reasoning_output_tokens") or 0),
                )
                cost = pricebook.cost(
                    {
                        "input": model_usage.input,
                        "cached": model_usage.cached,
                        "cache_write": model_usage.cache_write,
                        "output": model_usage.output,
                    },
                    name,
                    None,
                )
                entry["cost_usd"] += cost.usd or 0.0
                entry["priced_tokens"] += cost.priced_tokens
                entry["unpriced_tokens"] += cost.unpriced_tokens
                retained_cost += cost.usd or 0.0
                retained_priced += cost.priced_tokens
                retained_unpriced += cost.unpriced_tokens
            else:
                entry["cost_usd"] += float(model_row.get("retained_cost_usd") or 0.0)
                entry["priced_tokens"] += int(model_row.get("retained_priced_tokens") or 0)
                entry["unpriced_tokens"] += int(model_row.get("retained_unpriced_tokens") or 0)
                if recompute_costs:
                    retained_cost += float(model_row.get("retained_cost_usd") or 0.0)
                    retained_priced += int(model_row.get("retained_priced_tokens") or 0)
                    retained_unpriced += int(model_row.get("retained_unpriced_tokens") or 0)

    if lifetime.total < retained.total:
        lifetime = retained
    remainder = _difference(lifetime, retained)
    estimated_cost = 0.0
    estimated_priced = 0
    estimated_unpriced = 0
    if remainder.total:
        dominant = max(models.items(), key=lambda item: item[1]["total_tokens"])[0] if models else ""
        dominant = dominant or str(lifetime_segment.get("session_model") or "") or fallback_model or "unknown"
        if dominant == "unknown" and fallback_model:
            dominant = fallback_model
        cost = pricebook.cost(
            {
                "input": remainder.input,
                "cached": remainder.cached,
                "cache_write": remainder.cache_write,
                "output": remainder.output,
            },
            dominant,
            lifetime_segment.get("last_ts"),
        )
        estimated_cost = cost.usd or 0.0
        estimated_priced = cost.priced_tokens
        estimated_unpriced = cost.unpriced_tokens
        entry = models.setdefault(
            dominant,
            {
                "model": dominant,
                "calls": 0,
                "total_tokens": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "cost_usd": 0.0,
                "priced_tokens": 0,
                "unpriced_tokens": 0,
                "estimated_tokens": 0,
            },
        )
        entry["total_tokens"] += remainder.total
        entry["input_tokens"] += remainder.input
        entry["cached_input_tokens"] += remainder.cached
        entry["cache_write_input_tokens"] += remainder.cache_write
        entry["output_tokens"] += remainder.output
        entry["reasoning_output_tokens"] += remainder.reasoning
        entry["cost_usd"] += estimated_cost
        entry["priced_tokens"] += estimated_priced
        entry["unpriced_tokens"] += estimated_unpriced
        entry["estimated_tokens"] += remainder.total
    for entry in models.values():
        entry["estimated"] = entry["estimated_tokens"] > 0
    model_rows = sorted(models.values(), key=lambda item: item["total_tokens"], reverse=True)
    total_cost = retained_cost + estimated_cost
    return (
        lifetime,
        retained,
        model_rows,
        total_cost,
        estimated_cost,
        retained_priced + estimated_priced,
        retained_unpriced + estimated_unpriced,
    )


def collect_rows(
    *,
    codex_home: Path,
    archive_root: Path | None,
    index: AnalyticsIndex,
    pricebook: PriceBook,
    thread_source: str = "any",
    live_only: bool = False,
) -> list[SessionRow]:
    threads = load_threads(state_db(codex_home))
    ledger_records = load_ledger(archive_root)
    ledger_meta = ledger_index(ledger_records)
    segments = index.segments_by_session()
    mixes = index.model_mixes()

    live_map: dict[str, dict[str, Any]] = {}
    for file_row in index.live_files():
        session_id = str(file_row.get("session_id") or "")
        path = Path(str(file_row.get("path") or ""))
        if not session_id or not path.exists():
            continue
        entry = live_map.setdefault(
            session_id, {"bytes": 0, "paths": [], "mtime": 0}
        )
        entry["bytes"] += path.stat().st_size
        entry["paths"].append(path)
        entry["mtime"] = max(entry["mtime"], int(int(file_row.get("mtime_ns") or 0) / 1_000_000_000))

    archive_map: dict[str, dict[str, Any]] = {}
    archive_paths_seen: set[str] = set()
    for record in ledger_records:
        session_id = str(record.get("session_id") or "")
        path = Path(str(record.get("archive_path") or "")).expanduser()
        if not session_id or not path.exists():
            continue
        if str(path) in archive_paths_seen:
            continue
        archive_paths_seen.add(str(path))
        entry = archive_map.setdefault(
            session_id, {"bytes": 0, "source_bytes": 0, "paths": []}
        )
        entry["bytes"] += path.stat().st_size
        entry["source_bytes"] += int(record.get("src_size") or 0)
        entry["paths"].append(path)

    rows: dict[str, SessionRow] = {}
    all_ids = set(threads) | set(ledger_meta) | set(live_map) | set(archive_map) | set(segments)
    for session_id in all_ids:
        meta = threads.get(session_id)
        record = ledger_meta.get(session_id)
        row = _row_for(session_id, rows)
        row.name, row.name_source = display_name(meta, record)
        row.thread_source = meta.thread_source if meta else "unknown"
        row.model = meta.model if meta else ""
        row.cwd = (meta.cwd if meta else "") or str(record.get("cwd") if record else "")
        row.project = row.cwd
        row.created = meta.created_at if meta else 0
        row.updates = meta.updated_at if meta else 0
        row.cli_version = meta.cli_version if meta else ""
        row.agent_path = meta.agent_path if meta else ""
        row.agent_nickname = meta.agent_nickname if meta else ""
        if record and not row.updates:
            row.updates = int(record.get("last_activity_epoch") or 0)
        live = live_map.get(session_id)
        archive = archive_map.get(session_id)
        row.disk_bytes = int((live or {}).get("bytes") or 0) + int((archive or {}).get("bytes") or 0)
        row.source_bytes = int((live or {}).get("bytes") or 0) + int(
            (archive or {}).get("source_bytes") or 0
        )
        if live:
            row.rollout_path = str(live["paths"][0])
            row.updates = max(row.updates, int(live.get("mtime") or 0))
        if archive:
            row.archive_path = str(archive["paths"][0])
        if live and archive:
            row.source = "live+archive"
        elif live:
            row.source = "live"
        elif archive:
            row.source = "archive"
        else:
            row.source = "missing"

        session_segments = segments.get(session_id) or []
        row.segment_count = len(session_segments)
        if row.name == "(untitled)":
            message = next(
                (
                    str(segment.get("first_user_message") or "")
                    for segment in session_segments
                    if segment.get("first_user_message")
                ),
                "",
            )
            if meaningful(message, 12):
                row.name = " ".join(message.split())[:160]
                row.name_source = "rollout_head"
            else:
                project = Path(row.cwd).name if row.cwd else "unknown-project"
                row.name = f"(untitled) · {project} · {row.session_id[:8]}"
                row.name_source = "context"
        if session_segments:
            lifetime, retained, model_rows, cost, estimated_cost, priced, unpriced = _aggregate_segments(
                session_segments,
                pricebook,
                row.model,
                recompute_costs=not pricebook.apply_long_context,
            )
            row.tokens = lifetime.total
            row.retained_tokens = retained.total
            row.estimated_tokens = max(0, lifetime.total - retained.total)
            row.input_tokens = lifetime.input
            row.cached_input_tokens = lifetime.cached
            row.cache_write_input_tokens = lifetime.cache_write
            row.output_tokens = lifetime.output
            row.reasoning_output_tokens = lifetime.reasoning
            row.cost_usd = cost
            row.estimated_cost_usd = estimated_cost
            row.priced_tokens = priced
            row.unpriced_tokens = unpriced
            row.models = model_rows
            row.calls = sum(int(segment.get("calls") or 0) for segment in session_segments)
            row.model = max(model_rows, key=lambda item: item["total_tokens"])["model"] if model_rows else row.model
            row.thread_source = str(
                next(
                    (segment.get("thread_source") for segment in session_segments if segment.get("thread_source")),
                    row.thread_source,
                )
            )
            last_ts = max(
                (str(segment.get("last_ts") or "") for segment in session_segments), default=""
            )
            row.updates = max(row.updates, _epoch(last_ts))
            row.cost_basis = "parsed+estimate" if row.estimated_tokens else "parsed"
        else:
            total = int(meta.tokens_used if meta else 0)
            row.tokens = total
            row.estimated_tokens = total
            model = row.model
            if total > 0 and model:
                mix = mixes.get(model, ModelMix())
                estimated = pricebook.estimate_total(total, model, mix)
                row.cost_usd = estimated.usd
                row.estimated_cost_usd = estimated.usd or 0.0
                row.priced_tokens = estimated.priced_tokens
                row.unpriced_tokens = estimated.unpriced_tokens
                row.cost_basis = "model_average"
                row.models = [
                    {
                        "model": model,
                        "calls": 0,
                        "total_tokens": total,
                        "input_tokens": max(0, total - int(round(total * mix.output_share))),
                        "output_tokens": int(round(total * mix.output_share)),
                        "cost_usd": estimated.usd,
                        "priced_tokens": estimated.priced_tokens,
                        "unpriced_tokens": estimated.unpriced_tokens,
                        "estimated_tokens": total,
                        "estimated": True,
                    }
                ]
            elif total > 0:
                row.models = [
                    {
                        "model": model or "unknown",
                        "calls": 0,
                        "total_tokens": total,
                        "estimated_tokens": total,
                        "estimated": True,
                    }
                ]
            else:
                row.cost_basis = "none"

    values = list(rows.values())
    if live_only:
        values = [row for row in values if "live" in row.source]
    if thread_source != "any":
        values = [row for row in values if row.thread_source == thread_source]
    return values


def summarize(rows: Iterable[SessionRow]) -> dict[str, Any]:
    rows = list(rows)
    by_kind: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    total_disk = 0
    total_source = 0
    total_tokens = 0
    retained_tokens = 0
    estimated_tokens = 0
    total_cost = 0.0
    priced = 0
    unpriced = 0
    for row in rows:
        total_disk += row.disk_bytes
        total_source += row.source_bytes
        total_tokens += row.tokens
        retained_tokens += row.retained_tokens
        estimated_tokens += row.estimated_tokens
        total_cost += row.cost_usd or 0.0
        priced += row.priced_tokens
        unpriced += row.unpriced_tokens
        kind = by_kind.setdefault(
            row.thread_source,
            {"sessions": 0, "tokens": 0, "cost_usd": 0.0, "disk_bytes": 0},
        )
        kind["sessions"] += 1
        kind["tokens"] += row.tokens
        kind["cost_usd"] += row.cost_usd or 0.0
        kind["disk_bytes"] += row.disk_bytes
        for model in row.models:
            name = str(model.get("model") or row.model or "unknown")
            entry = by_model.setdefault(
                name,
                {
                    "sessions": set(),
                    "calls": 0,
                    "tokens": 0,
                    "cost_usd": 0.0,
                    "priced_tokens": 0,
                    "unpriced_tokens": 0,
                    "estimated_tokens": 0,
                },
            )
            entry["sessions"].add(row.session_id)
            entry["calls"] += int(model.get("calls") or 0)
            entry["tokens"] += int(model.get("total_tokens") or 0)
            entry["cost_usd"] += float(model.get("cost_usd") or 0.0)
            entry["priced_tokens"] += int(model.get("priced_tokens") or 0)
            entry["unpriced_tokens"] += int(model.get("unpriced_tokens") or 0)
            entry["estimated_tokens"] += int(model.get("estimated_tokens") or 0)
    model_rows = []
    for name, entry in by_model.items():
        model_rows.append(
            {
                "model": name,
                "sessions": len(entry["sessions"]),
                "calls": entry["calls"],
                "tokens": entry["tokens"],
                "cost_usd": entry["cost_usd"],
                "priced_tokens": entry["priced_tokens"],
                "unpriced_tokens": entry["unpriced_tokens"],
                "estimated_tokens": entry["estimated_tokens"],
            }
        )
    model_rows.sort(key=lambda item: item["cost_usd"], reverse=True)
    return {
        "sessions": len(rows),
        "detailed_sessions": sum(1 for row in rows if row.cost_basis == "parsed"),
        "exact_sessions": sum(1 for row in rows if row.cost_basis == "parsed"),
        "lifetime_estimate_sessions": sum(
            1 for row in rows if row.cost_basis == "parsed+estimate"
        ),
        "db_estimate_sessions": sum(1 for row in rows if row.cost_basis == "model_average"),
        "estimated_sessions": sum(
            1 for row in rows if row.cost_basis in {"parsed+estimate", "model_average"}
        ),
        "missing_usage_sessions": sum(1 for row in rows if row.cost_basis == "none"),
        "live_sessions": sum(1 for row in rows if "live" in row.source),
        "archive_sessions": sum(1 for row in rows if "archive" in row.source),
        "missing_sessions": sum(1 for row in rows if row.source == "missing"),
        "disk_bytes": total_disk,
        "source_bytes": total_source,
        "tokens": total_tokens,
        "retained_tokens": retained_tokens,
        "estimated_tokens": estimated_tokens,
        "cost_usd": total_cost,
        "priced_tokens": priced,
        "unpriced_tokens": unpriced,
        "by_kind": by_kind,
        "by_model": model_rows,
    }


def human_bytes(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def human_tokens(value: int | float) -> str:
    number = float(value)
    for suffix, scale in (("B", 1_000_000_000), ("M", 1_000_000), ("k", 1_000)):
        if abs(number) >= scale:
            return f"{number / scale:.2f}{suffix}"
    return f"{int(number)}"


def human_usd(value: float | None) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "$0.00"
    if abs(value) >= 1000:
        return f"${value:,.2f}"
    if abs(value) >= 1:
        return f"${value:.2f}"
    return f"${value:.4f}"


def _short_id(session_id: str) -> str:
    return session_id[:8]


def _kind(value: str) -> str:
    return {"user": "user", "subagent": "sub", "guardian_review": "review"}.get(
        value, value[:6] or "?"
    )


def _updated(value: int) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d")


def render_table(rows: list[SessionRow], limit: int = 30) -> str:
    shown = rows if limit <= 0 else rows[:limit]
    name_width = 44
    lines = [
        f"{'name':<{name_width}} {'session':<8} {'kind':<6} {'model':<22} {'disk':>9} {'tokens':>10} {'cost':>13} {'calls':>7}  updated"
    ]
    lines.append("-" * 148)
    for row in shown:
        label = row.label()
        name = label if len(label) <= name_width else label[: name_width - 1] + "~"
        model = row.model_summary()
        model = model if len(model) <= 22 else model[:21] + "~"
        cost_value = row.cost_usd
        if cost_value == 0 and row.unpriced_tokens and not row.priced_tokens:
            cost_value = None
        cost = human_usd(cost_value)
        if row.estimated_tokens and cost_value is not None:
            cost = "~" + cost
        lines.append(
            f"{name:<{name_width}} {_short_id(row.session_id):<8} {_kind(row.thread_source):<6} "
            f"{model:<22} {human_bytes(row.disk_bytes):>9} {human_tokens(row.tokens):>10} "
            f"{cost:>13} {row.calls:>7}  {_updated(row.updated)}"
        )
    if limit > 0 and len(rows) > limit:
        lines.append(f"... {len(rows) - limit:,} more sessions (use --limit 0 for all)")
    elif not rows:
        lines.append("(no sessions matched)")
    return "\n".join(lines)


def summary_text(summary: dict[str, Any]) -> str:
    lines = [
        f"sessions: {summary['sessions']:,}  "
        f"exact: {summary.get('exact_sessions', summary['detailed_sessions']):,}  "
        f"lifetime estimate: {summary.get('lifetime_estimate_sessions', 0):,}  "
        f"DB estimate: {summary.get('db_estimate_sessions', 0):,}  "
        f"no usage: {summary['missing_usage_sessions']:,}",
        f"disk: {human_bytes(summary['disk_bytes'])}  "
        f"source rollouts: {human_bytes(summary['source_bytes'])}  "
        f"live: {summary['live_sessions']:,}  archive: {summary['archive_sessions']:,}  "
        f"missing: {summary['missing_sessions']:,}",
        f"tokens: {human_tokens(summary['tokens'])}  "
        f"retained detail: {human_tokens(summary['retained_tokens'])}  "
        f"estimated remainder: {human_tokens(summary['estimated_tokens'])}",
        f"estimated cost: {human_usd(summary['cost_usd'])}  "
        f"priced tokens: {human_tokens(summary['priced_tokens'])}  "
        f"unpriced tokens: {human_tokens(summary['unpriced_tokens'])}",
    ]
    kinds = summary.get("by_kind") or {}
    if kinds:
        lines.append(
            "kinds: "
            + "  ".join(
                f"{name}={entry['sessions']} ({human_usd(entry['cost_usd'])})"
                for name, entry in sorted(kinds.items(), key=lambda item: -item[1]["sessions"])
            )
        )
    models = summary.get("by_model") or []
    if models:
        lines.append("top models:")
        for entry in models[:10]:
            marker = "*" if entry["unpriced_tokens"] else " "
            lines.append(
                f"  {entry['model'][:34]:<34} sessions={entry['sessions']:>5}  "
                f"tokens={human_tokens(entry['tokens']):>9}  cost={human_usd(entry['cost_usd']):>12}{marker}"
            )
    lines.append(
        "~ marks a cost that includes tokens estimated from the thread's lifetime total; "
        "`*` marks tokens with no price entry."
    )
    lines.append(
        "Tokens are cumulative prompt usage across API calls, not unique conversation text; "
        "the same context is billed again on each call."
    )
    return "\n".join(lines)


SESSIONS_COLUMNS = [
    "session_id",
    "name",
    "name_source",
    "thread_source",
    "model",
    "project",
    "source",
    "disk_bytes",
    "source_bytes",
    "tokens",
    "retained_tokens",
    "estimated_tokens",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "cost_usd",
    "estimated_cost_usd",
    "priced_tokens",
    "unpriced_tokens",
    "cost_basis",
    "calls",
    "segment_count",
    "created",
    "updated",
    "rollout_path",
    "archive_path",
    "cli_version",
    "agent_path",
    "agent_nickname",
]


def write_reports(
    rows: list[SessionRow], summary: dict[str, Any], out_dir: Path
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    sessions_json = out_dir / "sessions.json"
    sessions_json.write_text(
        json.dumps([row.serialize() for row in rows], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    paths["sessions_json"] = sessions_json

    sessions_csv = out_dir / "sessions.csv"
    with sessions_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SESSIONS_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row.serialize())
    paths["sessions_csv"] = sessions_csv

    model_rows = []
    for row in rows:
        for model in row.models:
            model_rows.append(
                {
                    "session_id": row.session_id,
                    "session_name": row.name,
                    "model": model.get("model"),
                    "calls": model.get("calls"),
                    "total_tokens": model.get("total_tokens"),
                    "input_tokens": model.get("input_tokens"),
                    "cached_input_tokens": model.get("cached_input_tokens"),
                    "output_tokens": model.get("output_tokens"),
                    "cost_usd": model.get("cost_usd"),
                    "priced_tokens": model.get("priced_tokens"),
                    "unpriced_tokens": model.get("unpriced_tokens"),
                    "estimated_tokens": model.get("estimated_tokens"),
                    "estimated": bool(model.get("estimated")),
                }
            )
    models_csv = out_dir / "session_models.csv"
    model_columns = [
        "session_id",
        "session_name",
        "model",
        "calls",
        "total_tokens",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "cost_usd",
        "priced_tokens",
        "unpriced_tokens",
        "estimated_tokens",
        "estimated",
    ]
    with models_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=model_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(model_rows)
    paths["models_csv"] = models_csv

    summary_json = out_dir / "summary.json"
    summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["summary_json"] = summary_json
    return paths


def plot_overview(rows: list[SessionRow], out_dir: Path, top: int = 15) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    colors = {"user": "#2b6cb0", "subagent": "#dd6b20", "guardian_review": "#805ad5"}

    cost_rows = [row for row in rows if row.cost_usd and row.cost_usd > 0]
    metric = "cost_usd" if cost_rows else "tokens"
    ranked = sorted(rows, key=lambda row: getattr(row, metric) or 0, reverse=True)[:top]
    ranked = [row for row in ranked if (getattr(row, metric) or 0) > 0]
    if ranked:
        fig, axis = plt.subplots(figsize=(11, max(4, 0.36 * len(ranked) + 1.5)))
        labels = [f"{row.label()[:48]}  [{_short_id(row.session_id)}]" for row in reversed(ranked)]
        values = [getattr(row, metric) or 0 for row in reversed(ranked)]
        bar_colors = [colors.get(row.thread_source, "#4a5568") for row in reversed(ranked)]
        axis.barh(labels, values, color=bar_colors)
        axis.set_xlabel("estimated USD" if metric == "cost_usd" else "tokens")
        axis.set_title("Top sessions by estimated API cost" if metric == "cost_usd" else "Top sessions by tokens")
        axis.grid(axis="x", alpha=0.25)
        axis.tick_params(axis="y", labelsize=8)
        fig.tight_layout()
        path = out_dir / "top-sessions.png"
        fig.savefig(path, dpi=160)
        plt.close(fig)
        paths.append(path)

    model_totals: dict[str, dict[str, float]] = {}
    for row in rows:
        for model in row.models:
            name = str(model.get("model") or "unknown")
            entry = model_totals.setdefault(name, {"tokens": 0.0, "cost": 0.0})
            entry["tokens"] += float(model.get("total_tokens") or 0)
            entry["cost"] += float(model.get("cost_usd") or 0)
    metric = "cost" if any(value["cost"] > 0 for value in model_totals.values()) else "tokens"
    ranked_models = sorted(model_totals.items(), key=lambda item: item[1][metric], reverse=True)[:top]
    ranked_models = [item for item in ranked_models if item[1][metric] > 0]
    if ranked_models:
        fig, axis = plt.subplots(figsize=(10, max(3.8, 0.36 * len(ranked_models) + 1.5)))
        labels = [name for name, _ in reversed(ranked_models)]
        values = [value[metric] for _, value in reversed(ranked_models)]
        axis.barh(labels, values, color="#2c7a7b")
        axis.set_xlabel("estimated USD" if metric == "cost" else "tokens")
        axis.set_title("Estimated API cost by model" if metric == "cost" else "Tokens by model")
        axis.grid(axis="x", alpha=0.25)
        axis.tick_params(axis="y", labelsize=8)
        fig.tight_layout()
        path = out_dir / "cost-by-model.png"
        fig.savefig(path, dpi=160)
        plt.close(fig)
        paths.append(path)
    return paths
