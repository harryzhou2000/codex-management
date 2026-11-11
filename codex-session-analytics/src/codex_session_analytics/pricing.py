"""Token-cost estimation from an explicit, user-overridable price table."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

DEFAULT_PRICE_PATH = Path(__file__).with_name("pricing.json")


@dataclass(frozen=True)
class Cost:
    usd: float | None
    priced_tokens: int
    unpriced_tokens: int
    input_usd: float = 0.0
    cached_usd: float = 0.0
    cache_write_usd: float = 0.0
    output_usd: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class ModelMix:
    output_share: float = 0.05
    cached_share: float = 0.5


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class PriceBook:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.models: dict[str, dict[str, Any]] = data.get("models", {})
        self.aliases: dict[str, str] = {
            str(key).lower(): str(value) for key, value in data.get("aliases", {}).items()
        }
        self.long_context: dict[str, Any] = data.get("long_context") or {}
        self.zero_cost_prefixes: tuple[str, ...] = tuple(
            str(value).lower() for value in data.get("zero_cost_prefixes", [])
        )

    @classmethod
    def load(cls, override: str | Path | None = None) -> "PriceBook":
        data = json.loads(DEFAULT_PRICE_PATH.read_text(encoding="utf-8"))
        if override:
            payload = json.loads(Path(override).expanduser().read_text(encoding="utf-8"))
            data = _merge(data, payload)
        return cls(data)

    def resolve(self, model: str | None) -> str | None:
        if not model:
            return None
        value = model.strip()
        lowered = value.lower()
        if lowered in self.aliases:
            return self.aliases[lowered]
        for key in self.models:
            if key.lower() == lowered:
                return key
        if "/" in lowered:
            suffix = lowered.rsplit("/", 1)[-1]
            if suffix in self.aliases:
                return self.aliases[suffix]
            for key in self.models:
                if key.lower() == suffix:
                    return key
        return None

    def is_zero_cost(self, model: str | None) -> bool:
        lowered = (model or "").lower()
        return any(lowered.startswith(prefix) for prefix in self.zero_cost_prefixes)

    def _peak_active(self, when: str | None, peak: dict[str, Any]) -> bool:
        parsed = _parse_time(when)
        if parsed is None:
            return False
        if peak.get("weekdays_only", True) and parsed.weekday() >= 5:
            return False
        hour = parsed.hour + parsed.minute / 60.0 + parsed.second / 3600.0
        for start, end in peak.get("hours_utc", []):
            if float(start) <= hour < float(end):
                return True
        return False

    def _rates(
        self, model: str, tokens: Mapping[str, int], when: str | None
    ) -> tuple[dict[str, float], dict[str, Any]]:
        entry = self.models[model]
        rates = {
            "input": float(entry.get("input", 0.0)),
            "cached_input": float(entry.get("cached_input", entry.get("input", 0.0))),
            "cache_write": float(entry.get("cache_write", entry.get("input", 0.0))),
            "output": float(entry.get("output", 0.0)),
        }
        peak = entry.get("peak")
        if isinstance(peak, dict) and self._peak_active(when, peak):
            rates = {
                "input": float(peak.get("input", rates["input"])),
                "cached_input": float(peak.get("cached_input", rates["cached_input"])),
                "cache_write": float(peak.get("cache_write", rates["cache_write"])),
                "output": float(peak.get("output", rates["output"])),
            }
        long_policy = entry.get("long_context", self.long_context)
        if isinstance(long_policy, dict) and tokens.get("input", 0) > int(
            long_policy.get("threshold_input_tokens", 272000)
        ):
            rates["input"] *= float(long_policy.get("input_multiplier", 1.0))
            rates["cached_input"] *= float(long_policy.get("input_multiplier", 1.0))
            rates["cache_write"] *= float(long_policy.get("input_multiplier", 1.0))
            rates["output"] *= float(long_policy.get("output_multiplier", 1.0))
        return rates, entry

    def cost(
        self, tokens: Mapping[str, int], model: str | None, when: str | None = None
    ) -> Cost:
        input_tokens = max(0, int(tokens.get("input", 0)))
        cached = max(0, int(tokens.get("cached", 0)))
        cache_write = max(0, int(tokens.get("cache_write", 0)))
        output = max(0, int(tokens.get("output", 0)))
        total = input_tokens + output
        if self.is_zero_cost(model):
            return Cost(0.0, total, 0, note="local provider; no API charge")
        key = self.resolve(model)
        if key is None:
            return Cost(None, 0, total, note="model has no price entry")
        rates, _ = self._rates(key, {"input": input_tokens}, when)
        cached = min(cached, input_tokens)
        cache_write = min(cache_write, max(0, input_tokens - cached))
        uncached = max(0, input_tokens - cached - cache_write)
        input_usd = uncached * rates["input"] / 1_000_000
        cached_usd = cached * rates["cached_input"] / 1_000_000
        cache_write_usd = cache_write * rates["cache_write"] / 1_000_000
        output_usd = output * rates["output"] / 1_000_000
        return Cost(
            input_usd + cached_usd + cache_write_usd + output_usd,
            total,
            0,
            input_usd,
            cached_usd,
            cache_write_usd,
            output_usd,
        )

    def estimate_total(
        self, total_tokens: int, model: str | None, mix: ModelMix | None = None
    ) -> Cost:
        """Estimate from a session-level total when detailed per-call usage is unavailable."""

        total = max(0, int(total_tokens))
        if total == 0:
            return Cost(0.0, 0, 0, note="no token total")
        mix = mix or ModelMix()
        output = int(round(total * mix.output_share))
        input_tokens = total - output
        cached = int(round(input_tokens * mix.cached_share))
        return self.cost(
            {"input": input_tokens, "cached": cached, "cache_write": 0, "output": output},
            model,
        )

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for model, entry in sorted(self.models.items()):
            rows.append(
                {
                    "model": model,
                    "input": entry.get("input"),
                    "cached_input": entry.get("cached_input"),
                    "cache_write": entry.get("cache_write"),
                    "output": entry.get("output"),
                    "peak": entry.get("peak"),
                    "long_context": entry.get("long_context", self.long_context),
                }
            )
        return rows
