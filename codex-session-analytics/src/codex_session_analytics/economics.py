"""Amortized context-cycle cost estimates for different compact lengths."""

from __future__ import annotations

import csv
import json
from bisect import bisect_right
from pathlib import Path
from typing import Any

from .pricing import PriceBook


def parse_length(value: str) -> int:
    text = value.strip().lower().replace("_", "")
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1_000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    elif text.endswith("b"):
        multiplier, text = 1_000_000_000, text[:-1]
    return max(1, int(float(text) * multiplier))


def parse_lengths(value: str) -> list[int]:
    lengths = []
    for part in value.split(","):
        if part.strip():
            length = parse_length(part)
            if length not in lengths:
                lengths.append(length)
    return sorted(lengths)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _compaction_stats(calls: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Pair each compacted marker with the preceding and following token calls."""

    ordered = sorted(
        (str(call.get("timestamp") or ""), call)
        for call in calls
        if call.get("timestamp")
    )
    times = [timestamp for timestamp, _ in ordered]
    inputs = []
    outputs = []
    cached_ratios = []
    post_inputs = []
    for item in items:
        if item.get("kind") != "compaction" or not item.get("timestamp"):
            continue
        position = bisect_right(times, str(item["timestamp"])) - 1
        if position < 0:
            continue
        previous = ordered[position][1]
        next_position = position + 1
        following = ordered[next_position][1] if next_position < len(ordered) else None
        input_tokens = int(previous.get("input_tokens") or 0)
        if input_tokens <= 0:
            continue
        inputs.append(input_tokens)
        outputs.append(int(previous.get("output_tokens") or 0))
        cached_ratios.append(int(previous.get("cached_input_tokens") or 0) / input_tokens)
        if following is not None:
            post_inputs.append(int(following.get("input_tokens") or 0))
    replacement = [
        float(item.get("replacement_tokens") or 0)
        for item in items
        if item.get("kind") == "compaction" and int(item.get("replacement_tokens") or 0) > 0
    ]
    return {
        "count": len(inputs),
        "average_input_tokens": _mean(inputs),
        "average_output_tokens": _mean(outputs),
        "total_output_tokens": sum(outputs),
        "cache_ratio": _mean(cached_ratios),
        "average_post_compaction_input": _mean(post_inputs),
        "fallback_summary_tokens": _mean(replacement),
    }


def _cycle(
    compact_tokens: int,
    summary_tokens: int,
    growth_per_call: float,
    total_calls: int,
) -> tuple[int, int]:
    """Return (total prompt tokens, compaction count) for a fixed call count."""

    context = 0.0
    total_prompt = 0.0
    compactions = 0
    reset_to = min(float(summary_tokens), max(0.0, compact_tokens - 1.0))
    for _ in range(max(1, total_calls)):
        prompt = context + growth_per_call
        total_prompt += prompt
        context = prompt
        if context >= compact_tokens:
            context = reset_to
            compactions += 1
    return int(round(total_prompt)), compactions


def estimate(
    calls: list[dict[str, Any]],
    items: list[dict[str, Any]],
    pricebook: PriceBook,
    lengths: list[int],
    *,
    fixed_calls: int | None = None,
) -> dict[str, Any]:
    """Estimate a steady task cycle from observed summary and per-call growth."""

    compaction = _compaction_stats(calls, items)
    observed_compactions = int(compaction["count"])
    total_calls = max(1, fixed_calls or len(calls) - observed_compactions or 1)
    summary_tokens = int(
        round(
            compaction["average_post_compaction_input"]
            or compaction["fallback_summary_tokens"]
        )
    )
    compaction_output_tokens = int(round(compaction["average_output_tokens"]))
    item_growth = sum(
        float(item.get("est_tokens") or 0)
        for item in items
        if item.get("kind") != "compaction"
    )
    growth_per_call = item_growth / total_calls if total_calls else 0.0

    output_tokens = sum(int(call.get("output_tokens") or 0) for call in calls)
    work_output_tokens = max(0, output_tokens - int(compaction["total_output_tokens"]))
    output_per_call = work_output_tokens / total_calls if total_calls else 0.0
    input_tokens = sum(int(call.get("input_tokens") or 0) for call in calls)
    cached_tokens = sum(int(call.get("cached_input_tokens") or 0) for call in calls)
    cache_ratio = cached_tokens / input_tokens if input_tokens else 0.0
    compaction_cache_ratio = compaction["cache_ratio"] or cache_ratio

    model_totals: dict[str, int] = {}
    for call in calls:
        model = str(call.get("model") or "unknown")
        model_totals[model] = model_totals.get(model, 0) + int(call.get("input_tokens") or 0)
    dominant_model = max(model_totals, key=lambda key: model_totals[key]) if model_totals else "unknown"

    no_surcharge = pricebook.with_long_context(False)
    actual_with_surcharge = sum(float(call.get("cost_usd") or 0.0) for call in calls)
    actual_no_surcharge = 0.0
    for call in calls:
        cost = no_surcharge.cost(
            {
                "input": int(call.get("input_tokens") or 0),
                "cached": int(call.get("cached_input_tokens") or 0),
                "cache_write": int(call.get("cache_write_input_tokens") or 0),
                "output": int(call.get("output_tokens") or 0),
            },
            call.get("model"),
            call.get("timestamp"),
        )
        actual_no_surcharge += cost.usd or 0.0

    rows = []
    for length in lengths:
        total_prompt, compactions = _cycle(length, summary_tokens, growth_per_call, total_calls)
        total_output = int(round(output_per_call * total_calls))
        cached = int(round(total_prompt * cache_ratio))
        context_cost = no_surcharge.cost(
            {"input": total_prompt, "cached": cached, "cache_write": 0, "output": total_output},
            dominant_model,
            None,
        )
        summary_call_cost = no_surcharge.cost(
            {
                "input": length,
                "cached": int(round(length * compaction_cache_ratio)),
                "cache_write": 0,
                "output": compaction_output_tokens,
            },
            dominant_model,
            None,
        )
        summary_generation_cost = (summary_call_cost.usd or 0.0) * compactions
        total_cost = (context_cost.usd or 0.0) + summary_generation_cost
        rows.append(
            {
                "compact_tokens": length,
                "average_prompt_per_call": total_prompt / total_calls,
                "total_prompt_tokens": total_prompt,
                "total_output_tokens": total_output,
                "total_calls": total_calls,
                "total_api_calls": total_calls + compactions,
                "estimated_compactions": compactions,
                "context_cost_usd": context_cost.usd or 0.0,
                "summary_generation_cost_usd": summary_generation_cost,
                "cost_usd": total_cost,
                "cost_per_call_usd": total_cost / total_calls,
                "cost_per_api_call_usd": total_cost / max(1, total_calls + compactions),
                "unpriced_tokens": context_cost.unpriced_tokens + summary_call_cost.unpriced_tokens * compactions,
                "summary_fits": length > summary_tokens,
                "current": length == 380_000,
            }
        )

    current = next(
        (row for row in rows if row["compact_tokens"] == 380_000),
        max(rows, key=lambda row: row["compact_tokens"]) if rows else None,
    )
    for row in rows:
        baseline = current["cost_usd"] if current else 0.0
        row["savings_vs_current_pct"] = ((baseline - row["cost_usd"]) / baseline * 100) if baseline else 0.0
    rows.sort(key=lambda row: row["cost_usd"])
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank

    return {
        "calls": len(calls),
        "fixed_calls": total_calls,
        "observed_compactions": observed_compactions,
        "summary_tokens": summary_tokens,
        "compaction_input_tokens": compaction["average_input_tokens"],
        "compaction_output_tokens": compaction_output_tokens,
        "compaction_cache_ratio": compaction_cache_ratio,
        "growth_per_call": growth_per_call,
        "output_per_call": output_per_call,
        "cache_ratio": cache_ratio,
        "model": dominant_model,
        "actual_with_surcharge_usd": actual_with_surcharge,
        "actual_no_surcharge_usd": actual_no_surcharge,
        "rows": rows,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        f"model={result['model']}  fixed work calls={result['fixed_calls']:,}",
        f"average summary context after compaction: {result['summary_tokens']:,} tokens",
        f"observed compaction calls: {result['observed_compactions']:,}  "
        f"average input={result['compaction_input_tokens']:,.0f}  "
        f"output={result['compaction_output_tokens']:,}  "
        f"cache={result['compaction_cache_ratio']:.1%}",
        f"average growth per call: {result['growth_per_call']:,.0f} tokens  "
        f"average output/call: {result['output_per_call']:,.0f} tokens",
        f"observed cache ratio: {result['cache_ratio']:.1%}",
        f"actual sample: ${result['actual_with_surcharge_usd']:,.2f} with surcharge, "
        f"${result['actual_no_surcharge_usd']:,.2f} without surcharge",
        "",
        f"{'rank':>4} {'compact':>9} {'avg prompt/call':>16} {'work calls':>11} {'compactions':>12} "
        f"{'context $':>10} {'summary $':>10} {'total $':>10} {'/call':>9} {'vs 380k':>9}",
    ]
    for row in result["rows"]:
        lines.append(
            f"{row['rank']:>4} {row['compact_tokens'] / 1000:>8.0f}k "
            f"{row['average_prompt_per_call']:>16,.0f} {row['total_calls']:>11,} "
            f"{row['estimated_compactions']:>12,} "
            f"${row['context_cost_usd']:>8,.2f} ${row['summary_generation_cost_usd']:>8,.2f} "
            f"${row['cost_usd']:>8,.2f} ${row['cost_per_call_usd']:>7.3f} "
            f"{row['savings_vs_current_pct']:>8.1f}%"
            f"{ '  current' if row['current'] else ''}"
            f"{ '  below summary' if not row['summary_fits'] else ''}"
        )
    return "\n".join(lines)


def write_outputs(result: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "economics.json"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    csv_path = out_dir / "economics.csv"
    columns = [
        "compact_tokens",
        "rank",
        "average_prompt_per_call",
        "total_calls",
        "total_api_calls",
        "total_prompt_tokens",
        "total_output_tokens",
        "estimated_compactions",
        "context_cost_usd",
        "summary_generation_cost_usd",
        "cost_usd",
        "cost_per_call_usd",
        "cost_per_api_call_usd",
        "savings_vs_current_pct",
        "summary_fits",
        "unpriced_tokens",
        "current",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(result["rows"])
    return {"json": json_path, "csv": csv_path}
