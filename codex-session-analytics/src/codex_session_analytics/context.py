"""Context-history extraction and the context-message plot."""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .rollout import ContextItem, RolloutSummary, TokenUsage

ITEM_COLORS = {
    "message": "#2b6cb0",
    "agent_message": "#3182ce",
    "tool_call": "#dd6b20",
    "tool_output": "#805ad5",
    "reasoning": "#975a16",
    "compaction": "#c53030",
    "tool_schema": "#718096",
    "config": "#a0aec0",
    "other": "#4a5568",
}

STACK_ORDER = [
    "message",
    "agent_message",
    "tool_call",
    "tool_output",
    "reasoning",
    "tool_schema",
    "config",
    "other",
    "compaction",
]


def _cumulative(items: list[ContextItem]) -> list[int]:
    values: list[int] = []
    current = 0
    for item in items:
        if item.kind == "compaction":
            current = item.est_tokens
        else:
            current += item.est_tokens
        values.append(current)
    return values


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            parsed = datetime.strptime(text, "%Y-%m-%d").replace(
                tzinfo=datetime.now().astimezone().tzinfo
            )
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _filter_context(
    items: list[ContextItem],
    calls: list[Any],
    *,
    since: str | None,
    until: str | None,
    last_days: float | None,
    last_hours: float | None,
    last_messages: int | None,
    last_turns: int | None,
) -> tuple[list[ContextItem], list[Any], dict[str, Any]]:
    item_times = [(item, _parse_time(item.timestamp)) for item in items]
    call_times = [(call, _parse_time(call.timestamp)) for call in calls]
    references = [time for _, time in item_times + call_times if time is not None]
    reference = max(references) if references else datetime.now(timezone.utc)
    lower = _parse_time(since)
    upper = _parse_time(until)
    if last_days is not None:
        candidate = reference - timedelta(days=last_days)
        lower = candidate if lower is None else max(lower, candidate)
    if last_hours is not None:
        candidate = reference - timedelta(hours=last_hours)
        lower = candidate if lower is None else max(lower, candidate)

    def in_window(value: datetime | None) -> bool:
        if value is None:
            return lower is None and upper is None
        if lower is not None and value < lower:
            return False
        if upper is not None and value > upper:
            return False
        return True

    filtered_items = [item for item, time in item_times if in_window(time)]
    filtered_calls = [call for call, time in call_times if in_window(time)]
    if last_messages is not None and last_messages > 0:
        filtered_items = filtered_items[-last_messages:]
        first_times = [time for item in filtered_items if (time := _parse_time(item.timestamp))]
        if first_times:
            start = min(first_times)
            filtered_calls = [
                call
                for call in filtered_calls
                if _parse_time(call.timestamp) is None or _parse_time(call.timestamp) >= start
            ]
    if last_turns is not None and last_turns > 0 and filtered_items:
        order: list[str] = []
        for item in filtered_items:
            key = item.turn_id or "(untagged)"
            if key not in order:
                order.append(key)
        keep = set(order[-last_turns:])
        filtered_items = [
            item for item in filtered_items if (item.turn_id or "(untagged)") in keep
        ]
        filtered_calls = [
            call
            for call in filtered_calls
            if (call.turn_id or "(untagged)") in keep
        ]
    filters = {
        "since": lower.isoformat() if lower else None,
        "until": upper.isoformat() if upper else None,
        "last_days": last_days,
        "last_hours": last_hours,
        "last_messages": last_messages,
        "last_turns": last_turns,
    }
    return filtered_items, filtered_calls, filters


def _turn_rows(items: list[ContextItem], calls: list[Any]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def group(key: str) -> dict[str, Any]:
        if key not in groups:
            order.append(key)
            groups[key] = {
                "turn_id": key,
                "start": None,
                "end": None,
                "items": 0,
                "estimated_tokens": 0,
                "cumulative_tokens": 0,
                "kinds": {},
                "models": set(),
                "calls": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "cost_usd": 0.0,
            }
        return groups[key]

    cumulative = 0
    for item in items:
        key = item.turn_id or "(untagged)"
        entry = group(key)
        if item.timestamp:
            entry["start"] = entry["start"] or item.timestamp
            entry["end"] = item.timestamp
        entry["items"] += 1
        entry["estimated_tokens"] += item.est_tokens
        entry["kinds"][item.kind] = entry["kinds"].get(item.kind, 0) + item.est_tokens
        if item.model:
            entry["models"].add(item.model)
        if item.kind == "compaction":
            cumulative = item.est_tokens
        else:
            cumulative += item.est_tokens
        entry["cumulative_tokens"] = cumulative

    for call in calls:
        key = call.turn_id or "(untagged)"
        entry = group(key)
        entry["calls"] += 1
        entry["input_tokens"] += call.usage.input
        entry["cached_input_tokens"] += call.usage.cached
        entry["cache_write_input_tokens"] += call.usage.cache_write
        entry["output_tokens"] += call.usage.output
        entry["reasoning_output_tokens"] += call.usage.reasoning
        if call.cost:
            entry["cost_usd"] += call.cost.usd or 0.0
        if call.model:
            entry["models"].add(call.model)
        if call.timestamp:
            entry["start"] = entry["start"] or call.timestamp
            entry["end"] = call.timestamp

    rows = []
    for index, key in enumerate(order, start=1):
        entry = groups[key]
        entry["index"] = index
        entry["models"] = sorted(entry["models"])
        rows.append(entry)
    return rows


def _usage_from_calls(calls: list[Any]) -> TokenUsage:
    usage = TokenUsage()
    for call in calls:
        usage.add(call.usage)
    return usage


def _model_rows_from_calls(calls: list[Any]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for call in calls:
        entry = groups.setdefault(
            call.model,
            {
                "model": call.model,
                "calls": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "priced_tokens": 0,
                "unpriced_tokens": 0,
                "estimated_tokens": 0,
                "estimated": False,
            },
        )
        entry["calls"] += 1
        entry["input_tokens"] += call.usage.input
        entry["cached_input_tokens"] += call.usage.cached
        entry["output_tokens"] += call.usage.output
        entry["reasoning_output_tokens"] += call.usage.reasoning
        entry["total_tokens"] += call.usage.total
        if call.cost:
            entry["cost_usd"] += call.cost.usd or 0.0
            entry["priced_tokens"] += call.cost.priced_tokens
            entry["unpriced_tokens"] += call.cost.unpriced_tokens
        else:
            entry["unpriced_tokens"] += call.usage.total
    return sorted(groups.values(), key=lambda item: item["total_tokens"], reverse=True)


def build_payload(
    summary: RolloutSummary,
    *,
    name: str,
    thread_source: str,
    cwd: str,
    source: str,
    max_chars: int | None = 1000,
    granularity: str = "message",
    last_turns: int | None = None,
    last_messages: int | None = None,
    since: str | None = None,
    until: str | None = None,
    last_days: float | None = None,
    last_hours: float | None = None,
) -> dict[str, Any]:
    items, calls, filters = _filter_context(
        summary.items or [],
        summary.calls,
        since=since,
        until=until,
        last_days=last_days,
        last_hours=last_hours,
        last_messages=last_messages,
        last_turns=last_turns,
    )
    cumulative = _cumulative(items)
    turns = _turn_rows(items, calls)
    item_rows = []
    for item, total in zip(items, cumulative):
        text, truncated = item.preview(max_chars)
        item_rows.append(
            {
                "index": item.index,
                "ordinal": item.ordinal,
                "timestamp": item.timestamp,
                "kind": item.kind,
                "role": item.role,
                "name": item.name,
                "model": item.model,
                "turn_id": item.turn_id,
                "chars": item.chars,
                "est_tokens": item.est_tokens,
                "cumulative_tokens": total,
                "encrypted": item.encrypted,
                "image_count": item.image_count,
                "replacement_items": item.replacement_items,
                "replacement_tokens": item.replacement_tokens,
                "text": text,
                "text_truncated": truncated,
            }
        )
    filter_active = any(
        value is not None
        for key, value in filters.items()
        if key in {"since", "until", "last_days", "last_hours", "last_messages", "last_turns"}
    )
    call_rows = []
    for call in calls:
        call_rows.append(
            {
                "seq": call.seq,
                "timestamp": call.timestamp,
                "turn_id": call.turn_id,
                "model": call.model,
                "input_tokens": call.usage.input,
                "cached_input_tokens": call.usage.cached,
                "cache_write_input_tokens": call.usage.cache_write,
                "output_tokens": call.usage.output,
                "reasoning_output_tokens": call.usage.reasoning,
                "total_tokens": call.usage.total,
                "context_window": call.context_window,
                "cost_usd": call.cost.usd if call.cost else None,
                "source": call.source,
            }
        )
    compactions = [row for row in item_rows if row["kind"] == "compaction"]
    if filter_active:
        usage = _usage_from_calls(calls)
        models = _model_rows_from_calls(calls)
        selected_cost = sum(float(model["cost_usd"] or 0.0) for model in models)
        selected_priced = sum(int(model["priced_tokens"] or 0) for model in models)
        selected_unpriced = sum(int(model["unpriced_tokens"] or 0) for model in models)
        selected_retained = usage.total
        selected_estimated = 0
        selected_estimated_cost = 0.0
        selected_compactions = len(compactions)
    else:
        usage = summary.usage
        models = []
        for model, model_usage in summary.models.items():
            models.append(
                {
                    "model": model,
                    "calls": model_usage.calls,
                    "input_tokens": model_usage.usage.input,
                    "cached_input_tokens": model_usage.usage.cached,
                    "output_tokens": model_usage.usage.output,
                    "reasoning_output_tokens": model_usage.usage.reasoning,
                    "total_tokens": model_usage.usage.total,
                    "cost_usd": model_usage.cost_usd,
                    "priced_tokens": model_usage.priced_tokens,
                    "unpriced_tokens": model_usage.unpriced_tokens,
                    "estimated_tokens": model_usage.estimated_tokens,
                    "estimated": model_usage.estimated_tokens > 0,
                }
            )
        selected_cost = summary.cost_usd
        selected_priced = summary.priced_tokens
        selected_unpriced = summary.unpriced_tokens
        selected_retained = summary.retained_usage.total
        selected_estimated = max(0, usage.total - summary.retained_usage.total)
        selected_estimated_cost = summary.estimated_cost_usd
        selected_compactions = summary.compaction_count
    return {
        "session": {
            "session_id": summary.session_id,
            "name": name,
            "cwd": cwd or summary.cwd,
            "thread_source": thread_source or summary.thread_source,
            "model": summary.session_model,
            "models": models,
            "source": source,
            "path": ", ".join(str(path) for path in summary.paths) or str(summary.path),
            "cli_version": summary.cli_version,
            "first_timestamp": summary.first_ts,
            "last_timestamp": summary.last_ts,
            "raw_lifetime_tokens": summary.raw_thread_usage.total,
            "initial_counter_tokens": summary.initial_thread_usage.total,
        },
        "summary": {
            "calls": len(calls),
            "items": len(item_rows),
            "turns": len(turns),
            "input_tokens": usage.input,
            "cached_input_tokens": usage.cached,
            "cache_write_input_tokens": usage.cache_write,
            "output_tokens": usage.output,
            "reasoning_output_tokens": usage.reasoning,
            "total_tokens": usage.total,
            "retained_tokens": selected_retained,
            "estimated_tokens": selected_estimated,
            "cost_usd": selected_cost,
            "estimated_cost_usd": selected_estimated_cost,
            "priced_tokens": selected_priced,
            "unpriced_tokens": selected_unpriced,
            "context_window": summary.context_window,
            "compactions": selected_compactions,
            "filtered": filter_active,
            "lifetime_tokens": summary.usage.total,
            "lifetime_input_tokens": summary.usage.input,
            "lifetime_cached_input_tokens": summary.usage.cached,
            "lifetime_output_tokens": summary.usage.output,
            "lifetime_cost_usd": summary.cost_usd,
            "lifetime_compactions": summary.compaction_count,
        },
        "calls": call_rows,
        "items": item_rows,
        "compactions": compactions,
        "turns": turns,
        "granularity": granularity,
        "filters": filters,
    }


def render_preview(payload: dict[str, Any], head: int = 6, tail: int = 6) -> str:
    items = payload["items"]
    if not items:
        return "(no context items)"
    selected = items[:head]
    if tail and len(items) > head + tail:
        selected = selected + [None] + items[-tail:]
    lines = [
        f"{'idx':>6} {'kind':<12} {'role':<8} {'est_tok':>8} {'cum':>10}  text"
    ]
    for item in selected:
        if item is None:
            lines.append("...")
            continue
        text = " ".join(str(item["text"]).split())[:110]
        lines.append(
            f"{item['index']:>6} {item['kind']:<12} {item['role']:<8} "
            f"{item['est_tokens']:>8} {item['cumulative_tokens']:>10}  {text}"
        )
    return "\n".join(lines)


def render_turn_preview(payload: dict[str, Any], head: int = 8, tail: int = 8) -> str:
    turns = payload.get("turns") or []
    if not turns:
        return "(no turns)"
    selected = turns[:head]
    if tail and len(turns) > head + tail:
        selected = selected + [None] + turns[-tail:]
    lines = [
        f"{'turn':>5} {'items':>6} {'calls':>7} {'input_tok':>12} {'est_tok':>10} {'cum':>10}  turn_id"
    ]
    for turn in selected:
        if turn is None:
            lines.append("...")
            continue
        lines.append(
            f"{turn['index']:>5} {turn['items']:>6} {turn['calls']:>7} "
            f"{turn['input_tokens']:>12} {turn['estimated_tokens']:>10} "
            f"{turn['cumulative_tokens']:>10}  {turn['turn_id']}"
        )
    return "\n".join(lines)


def _short_id(value: str) -> str:
    return value[:8]


def _filter_label(filters: dict[str, Any]) -> str:
    parts = []
    if filters.get("last_turns"):
        parts.append(f"last {filters['last_turns']} turns")
    if filters.get("last_messages"):
        parts.append(f"last {filters['last_messages']} messages")
    if filters.get("last_days"):
        parts.append(f"last {filters['last_days']} days")
    if filters.get("last_hours"):
        parts.append(f"last {filters['last_hours']} hours")
    if filters.get("since"):
        parts.append(f"since {filters['since']}")
    if filters.get("until"):
        parts.append(f"until {filters['until']}")
    return ", ".join(parts)


def _stacked(items: list[dict[str, Any]], max_points: int) -> tuple[list[float], dict[str, list[float]], list[float]]:
    step = max(1, math.ceil(len(items) / max_points))
    bins = math.ceil(len(items) / step)
    xs: list[float] = []
    values: dict[str, list[float]] = {kind: [0.0] * bins for kind in STACK_ORDER}
    cumulative: list[float] = []
    for bucket in range(bins):
        chunk = items[bucket * step : (bucket + 1) * step]
        xs.append((bucket + 0.5) * step)
        for item in chunk:
            kind = str(item["kind"])
            if kind not in values:
                values[kind] = [0.0] * bins
            values[kind][bucket] += float(item["est_tokens"])
        cumulative.append(float(chunk[-1]["cumulative_tokens"] if chunk else 0))
    return xs, values, cumulative


def _stacked_turns(
    turns: list[dict[str, Any]]
) -> tuple[list[int], dict[str, list[float]], list[float]]:
    xs = [int(turn["index"]) for turn in turns]
    values: dict[str, list[float]] = {kind: [0.0] * len(turns) for kind in STACK_ORDER}
    for index, turn in enumerate(turns):
        for kind, value in (turn.get("kinds") or {}).items():
            values.setdefault(kind, [0.0] * len(turns))[index] += float(value)
    cumulative = [float(turn.get("cumulative_tokens") or 0) for turn in turns]
    return xs, values, cumulative


def plot_context(payload: dict[str, Any], out_path: Path, *, max_points: int = 20000) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    session = payload["session"]
    summary = payload["summary"]
    calls = payload["calls"]
    items = payload["items"]
    turns = payload.get("turns") or []
    granularity = payload.get("granularity", "message")

    figure = plt.figure(figsize=(15, 10.5), constrained_layout=True)
    grid = figure.add_gridspec(2, 1, height_ratios=[1.0, 1.15])
    top = figure.add_subplot(grid[0])
    bottom = figure.add_subplot(grid[1])

    if granularity == "turn" and turns:
        x = np.arange(1, len(turns) + 1)
        input_tokens = np.array([turn["input_tokens"] for turn in turns], dtype=float) / 1000.0
        cached = np.array([turn["cached_input_tokens"] for turn in turns], dtype=float) / 1000.0
        top.plot(x, input_tokens, color="#2b6cb0", linewidth=1.4, label="prompt tokens per turn")
        top.plot(x, cached, color="#dd6b20", linewidth=1.0, linestyle="--", label="cached input")
        top.fill_between(x, 0, cached, color="#dd6b20", alpha=0.12)
        # A turn sum can exceed the per-call window many times; do not compare it to that line.
        previous_models: tuple[str, ...] | None = None
        for index, turn in enumerate(turns, start=1):
            models = tuple(turn.get("models") or [])
            if previous_models is not None and models != previous_models:
                top.axvline(index, color="#4a5568", linewidth=0.8, linestyle=":")
            previous_models = models
        top.set_title("Prompt tokens sent to the model in each turn")
        top.set_xlabel("Turn")
        top.set_ylabel("tokens (thousands)")
        if len(turns) <= 60:
            top.set_xticks(x)
        else:
            top.xaxis.set_major_locator(MaxNLocator(integer=True))
        top.grid(alpha=0.2)
        top.legend(loc="upper left", fontsize=8)
    elif calls:
        x = np.arange(1, len(calls) + 1)
        input_tokens = np.array([call["input_tokens"] for call in calls], dtype=float) / 1000.0
        cached = np.array([call["cached_input_tokens"] for call in calls], dtype=float) / 1000.0
        top.plot(x, input_tokens, color="#2b6cb0", linewidth=1.4, label="input tokens (context)")
        top.plot(x, cached, color="#dd6b20", linewidth=1.0, linestyle="--", label="cached input")
        top.fill_between(x, 0, cached, color="#dd6b20", alpha=0.12)
        window = summary.get("context_window")
        if window:
            top.axhline(
                window / 1000.0,
                color="#718096",
                linewidth=0.9,
                linestyle=":",
                label=f"context window {window / 1000:.0f}k",
            )
        previous_model = None
        model_changes = 0
        for index, call in enumerate(calls, start=1):
            if previous_model is not None and call["model"] != previous_model:
                model_changes += 1
                if model_changes <= 12:
                    top.axvline(index, color="#4a5568", linewidth=0.8, linestyle=":")
                    top.text(
                        index,
                        top.get_ylim()[1] * 0.96,
                        str(call["model"])[:18],
                        rotation=90,
                        fontsize=6,
                        color="#4a5568",
                        va="top",
                        ha="right",
                    )
            previous_model = call["model"]
        for index in range(1, len(calls)):
            previous = calls[index - 1]["input_tokens"]
            current = calls[index]["input_tokens"]
            if previous > 0 and current < previous * 0.75:
                top.axvline(index + 1, color="#c53030", linewidth=1.0, linestyle="--", alpha=0.8)
        top.set_title("Context sent to the model on each API call")
        top.set_xlabel("API call")
        top.set_ylabel("tokens (thousands)")
        top.grid(alpha=0.2)
        top.legend(loc="upper left", fontsize=8)
    else:
        top.text(0.5, 0.5, "No token-usage records in this rollout", ha="center", va="center", transform=top.transAxes)
        top.set_axis_off()

    if granularity == "turn" and turns:
        xs, values, cumulative = _stacked_turns(turns)
        bottoms = np.zeros(len(xs))
        for kind in STACK_ORDER:
            series = np.array(values.get(kind, [0.0] * len(xs)), dtype=float)
            if series.any():
                bottom.bar(
                    xs,
                    series,
                    bottom=bottoms,
                    width=0.8,
                    color=ITEM_COLORS.get(kind, "#4a5568"),
                    label=kind,
                )
                bottoms += series
        bottom.set_title("Context items and cumulative estimate by turn")
        bottom.set_xlabel("Turn")
        bottom.set_ylabel("estimated tokens per turn")
        if len(turns) <= 60:
            bottom.set_xticks(xs)
        else:
            bottom.xaxis.set_major_locator(MaxNLocator(integer=True))
        bottom.grid(axis="y", alpha=0.18)
        bottom.legend(loc="upper left", fontsize=7, ncol=3)
        right = bottom.twinx()
        right.plot(xs, cumulative, color="#1a202c", linewidth=1.1, alpha=0.8, label="cumulative estimate")
        right.set_ylabel("cumulative estimated tokens")
        right.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1000:.0f}k"))
        right.legend(loc="upper right", fontsize=7)
    elif items:
        if len(items) <= max_points:
            xs = [item["index"] for item in items]
            bottoms = np.zeros(len(items))
            for kind in STACK_ORDER:
                values = np.array([item["est_tokens"] if item["kind"] == kind else 0 for item in items], dtype=float)
                if values.any():
                    bottom.bar(xs, values, bottom=bottoms, width=1.0, color=ITEM_COLORS.get(kind, "#4a5568"), label=kind)
                    bottoms += values
            cumulative = np.array([item["cumulative_tokens"] for item in items], dtype=float)
        else:
            xs, values, cumulative = _stacked(items, max_points)
            bottoms = np.zeros(len(xs))
            width = max(1.0, len(items) / max(1, len(xs)) * 0.9)
            for kind in STACK_ORDER:
                series = np.array(values.get(kind, [0.0] * len(xs)), dtype=float)
                if series.any():
                    bottom.bar(xs, series, bottom=bottoms, width=width, color=ITEM_COLORS.get(kind, "#4a5568"), label=kind)
                    bottoms += series
        bottom.set_title("Context items and cumulative estimated context")
        bottom.set_xlabel("message / item index")
        bottom.set_ylabel("estimated tokens per item")
        bottom.grid(axis="y", alpha=0.18)
        bottom.legend(loc="upper left", fontsize=7, ncol=3)
        right = bottom.twinx()
        right.plot(xs, cumulative, color="#1a202c", linewidth=1.1, alpha=0.8, label="cumulative estimate")
        right.set_ylabel("cumulative estimated tokens")
        right.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1000:.0f}k"))
        right.legend(loc="upper right", fontsize=7)
    else:
        bottom.text(0.5, 0.5, "No context items in this rollout", ha="center", va="center", transform=bottom.transAxes)
        bottom.set_axis_off()

    model_text = ", ".join(str(model["model"]) for model in session.get("models") or []) or session.get("model") or "?"
    subtitle = (
        f"{session.get('name', '(untitled)')}  [{_short_id(str(session.get('session_id', '')))}]  "
        f"models: {model_text}\n"
        f"calls={summary['calls']:,}  items={summary['items']:,}  turns={summary.get('turns', 0):,}  "
        f"input={summary['input_tokens'] / 1_000_000:.2f}M  cached={summary['cached_input_tokens'] / 1_000_000:.2f}M  "
        f"output={summary['output_tokens'] / 1_000_000:.2f}M  "
        f"cost={'~' if summary.get('estimated_tokens') else ''}${summary['cost_usd']:.4f}  "
        f"compactions={summary['compactions']}"
        + (f"  filter: {_filter_label(payload.get('filters') or {})}" if _filter_label(payload.get('filters') or {}) else "")
    )
    figure.suptitle(subtitle, fontsize=10, x=0.01, ha="left")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out_path, dpi=160)
    plt.close(figure)
    return out_path


def write_context_outputs(
    payload: dict[str, Any], out_dir: Path, *, plot: bool = True, max_points: int = 20000
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    json_path = out_dir / "context.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["json"] = json_path

    csv_path = out_dir / "context.csv"
    columns = [
        "index",
        "ordinal",
        "timestamp",
        "kind",
        "role",
        "name",
        "model",
        "turn_id",
        "chars",
        "est_tokens",
        "cumulative_tokens",
        "encrypted",
        "image_count",
        "replacement_items",
        "replacement_tokens",
        "text",
        "text_truncated",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(payload["items"])
    paths["csv"] = csv_path

    if payload.get("turns"):
        turns_path = out_dir / "context_turns.csv"
        turn_columns = [
            "index",
            "turn_id",
            "start",
            "end",
            "items",
            "calls",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "estimated_tokens",
            "cumulative_tokens",
            "cost_usd",
            "models",
        ]
        with turns_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=turn_columns, extrasaction="ignore")
            writer.writeheader()
            for turn in payload["turns"]:
                row = dict(turn)
                row["models"] = "; ".join(turn.get("models") or [])
                writer.writerow(row)
        paths["turns_csv"] = turns_path

    md_path = out_dir / "context.md"
    md_path.write_text(_markdown(payload), encoding="utf-8")
    paths["markdown"] = md_path

    if plot:
        paths["plot"] = plot_context(payload, out_dir / "context.png", max_points=max_points)
    return paths


def _markdown(payload: dict[str, Any]) -> str:
    session = payload["session"]
    summary = payload["summary"]
    lines = [
        f"# {session.get('name') or '(untitled)'}",
        "",
        f"- Session: `{session['session_id']}`",
        f"- Context: `{session.get('cwd')}`",
        f"- Source: {session.get('source')} `{session.get('path')}`",
        f"- Models: {', '.join(str(model['model']) for model in session.get('models') or []) or session.get('model')}",
        f"- Granularity: {payload.get('granularity', 'message')}  "
        f"Filters: {_filter_label(payload.get('filters') or {}) or 'none'}",
        f"- Calls: {summary['calls']}  Items: {summary['items']}  Turns: {summary.get('turns', 0)}  "
        f"Compactions: {summary['compactions']}",
        f"- Selected tokens: input {summary['input_tokens']:,}, cached {summary['cached_input_tokens']:,}, "
        f"output {summary['output_tokens']:,}; selected cost ${summary['cost_usd']:.4f}",
        f"- Lifetime tokens: {summary.get('lifetime_tokens', 0):,}; lifetime cost "
        f"${summary.get('lifetime_cost_usd', 0):.4f} (retained {summary['retained_tokens']:,}, "
        f"estimated remainder {summary['estimated_tokens']:,})",
        f"- Estimated item tokens use a character/CJK heuristic; actual call context comes from usage records.",
        "",
    ]
    turns = payload.get("turns") or []
    if turns:
        lines.extend(
            [
                "## Turns",
                "",
                "| turn | items | calls | input tokens | est tokens | cumulative | turn id |",
                "|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for turn in turns[:25]:
            lines.append(
                f"| {turn['index']} | {turn['items']} | {turn['calls']} | {turn['input_tokens']:,} | "
                f"{turn['estimated_tokens']:,} | {turn['cumulative_tokens']:,} | {turn['turn_id']} |"
            )
    lines.extend(
        [
        "",
        "## Largest context items",
        "",
        "| idx | kind | role | est tokens | cumulative | preview |",
        "|---:|---|---|---:|---:|---|",
        ]
    )
    largest = sorted(payload["items"], key=lambda item: item["est_tokens"], reverse=True)[:20]
    for item in largest:
        preview = " ".join(str(item["text"]).split()).replace("|", "\\|")[:160]
        lines.append(
            f"| {item['index']} | {item['kind']} | {item['role']} | {item['est_tokens']:,} | "
            f"{item['cumulative_tokens']:,} | {preview} |"
        )
    if payload["compactions"]:
        lines.extend(["", "## Compactions", ""])
        for item in payload["compactions"]:
            lines.append(
                f"- index {item['index']}: {item['replacement_items']} replacement items, "
                f"estimated {item['replacement_tokens']:,} tokens"
            )
    return "\n".join(lines) + "\n"
