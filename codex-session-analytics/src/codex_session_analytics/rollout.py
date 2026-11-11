"""Read rollout JSONL files, including zstd-compressed archives, without mutating them."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, TextIO

from .paths import session_id_from_path
from .pricing import Cost, PriceBook

TURN_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"')
TURN_ID_RE = re.compile(r'"turn_id"\s*:\s*"([^"]+)"')
META_SESSION_RE = re.compile(r'"session_id"\s*:\s*"([^"]+)"')
META_ID_RE = re.compile(r'"id"\s*:\s*"([^"]+)"')
META_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
META_PROVIDER_RE = re.compile(r'"model_provider"\s*:\s*"([^"]+)"')
META_THREAD_RE = re.compile(r'"thread_source"\s*:\s*"([^"]+)"')
META_CLI_RE = re.compile(r'"cli_version"\s*:\s*"([^"]+)"')

IMAGE_TOKEN_ESTIMATE = 1024
ITEM_OVERHEAD_TOKENS = 4


@contextmanager
def open_jsonl(path: Path) -> Iterator[TextIO]:
    """Yield a text stream for a plain or .zst rollout."""

    if path.suffix == ".zst":
        if not shutil.which("zstd"):
            raise RuntimeError("zstd is required to read compressed session archives")
        process = subprocess.Popen(
            ["zstd", "-dc", str(path)],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        try:
            yield process.stdout
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            process.wait()
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        yield handle


def _record_type(raw: str) -> str | None:
    head = raw[:220]
    for record_type in (
        "token_usage_record",
        "session_meta",
        "turn_context",
        "compacted",
        "event_msg",
        "response_item",
    ):
        if f'"type":"{record_type}"' in head or f'"type": "{record_type}"' in head:
            return record_type
    return None


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass
class TokenUsage:
    input: int = 0
    cached: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input += other.input
        self.cached += other.cached
        self.cache_write += other.cache_write
        self.output += other.output
        self.reasoning += other.reasoning

    @property
    def total(self) -> int:
        return self.input + self.output

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "cached": self.cached,
            "cache_write": self.cache_write,
            "output": self.output,
            "reasoning": self.reasoning,
            "total": self.total,
        }


def usage_from_dict(value: dict[str, Any] | None) -> TokenUsage:
    value = value or {}
    return TokenUsage(
        input=_int(value.get("input_tokens")),
        cached=_int(value.get("cached_input_tokens")),
        cache_write=_int(value.get("cache_write_input_tokens")),
        output=_int(value.get("output_tokens")),
        reasoning=_int(value.get("reasoning_output_tokens")),
    )


@dataclass
class TokenCall:
    seq: int
    timestamp: str | None
    turn_id: str | None
    model: str
    usage: TokenUsage
    response_id: str = ""
    context_window: int | None = None
    source: str = "token_usage_record"
    cost: Cost | None = None


@dataclass
class ModelUsage:
    model: str
    calls: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    retained_usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    priced_tokens: int = 0
    unpriced_tokens: int = 0
    retained_cost_usd: float = 0.0
    retained_priced_tokens: int = 0
    retained_unpriced_tokens: int = 0
    estimated_tokens: int = 0

    def add(self, call: TokenCall) -> None:
        self.calls += 1
        self.usage.add(call.usage)
        self.retained_usage.add(call.usage)
        if call.cost:
            self.priced_tokens += call.cost.priced_tokens
            self.unpriced_tokens += call.cost.unpriced_tokens
            self.cost_usd += call.cost.usd or 0.0
            self.retained_priced_tokens += call.cost.priced_tokens
            self.retained_unpriced_tokens += call.cost.unpriced_tokens
            self.retained_cost_usd += call.cost.usd or 0.0


@dataclass
class ContextItem:
    index: int
    ordinal: int | None
    timestamp: str | None
    kind: str
    role: str = ""
    name: str = ""
    text: str = ""
    chars: int = 0
    est_tokens: int = 0
    turn_id: str | None = None
    model: str = ""
    call_id: str | None = None
    encrypted: bool = False
    image_count: int = 0
    replacement_items: int = 0
    replacement_tokens: int = 0

    def preview(self, max_chars: int | None) -> tuple[str, bool]:
        if max_chars is None or max_chars <= 0 or len(self.text) <= max_chars:
            return self.text, False
        return self.text[:max_chars], True


@dataclass
class RolloutSummary:
    session_id: str
    path: Path
    cwd: str = ""
    session_model: str = ""
    model_provider: str = ""
    thread_source: str = ""
    cli_version: str = ""
    context_window: int | None = None
    compaction_count: int = 0
    calls: list[TokenCall] = field(default_factory=list)
    models: dict[str, ModelUsage] = field(default_factory=dict)
    items: list[ContextItem] | None = None
    thread_usage: TokenUsage = field(default_factory=TokenUsage)
    raw_thread_usage: TokenUsage = field(default_factory=TokenUsage)
    initial_thread_usage: TokenUsage = field(default_factory=TokenUsage)
    initial_call_usage: TokenUsage = field(default_factory=TokenUsage)
    thread_usage_ts: str | None = None
    paths: list[Path] = field(default_factory=list)
    first_user_message: str = ""
    first_ts: str | None = None
    last_ts: str | None = None

    @property
    def usage(self) -> TokenUsage:
        total = TokenUsage()
        for model in self.models.values():
            total.add(model.usage)
        return total

    @property
    def cost_usd(self) -> float:
        return sum(model.cost_usd for model in self.models.values())

    @property
    def priced_tokens(self) -> int:
        return sum(model.priced_tokens for model in self.models.values())

    @property
    def unpriced_tokens(self) -> int:
        return sum(model.unpriced_tokens for model in self.models.values())

    @property
    def retained_usage(self) -> TokenUsage:
        total = TokenUsage()
        for call in self.calls:
            total.add(call.usage)
        return total

    @property
    def retained_cost_usd(self) -> float:
        return sum(model.retained_cost_usd for model in self.models.values())

    @property
    def retained_priced_tokens(self) -> int:
        return sum(model.retained_priced_tokens for model in self.models.values())

    @property
    def retained_unpriced_tokens(self) -> int:
        return sum(model.retained_unpriced_tokens for model in self.models.values())

    @property
    def estimated_tokens(self) -> int:
        return sum(model.estimated_tokens for model in self.models.values())

    @property
    def estimated_cost_usd(self) -> float:
        return max(0.0, self.cost_usd - self.retained_cost_usd)


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic estimate: CJK characters ~1 token, other text ~4 chars/token."""

    if not text:
        return 0
    cjk = 0
    for char in text:
        code = ord(char)
        if (
            0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
        ):
            cjk += 1
    other = len(text) - cjk
    return max(1, int(round(cjk + other / 4.0)))


def _headline(text: str) -> str:
    value = " ".join((text or "").split())
    if len(value) < 12:
        return ""
    lowered = value.lower().rstrip(" .")
    if lowered in {
        ".",
        "..",
        "...",
        "?",
        "ok",
        "okay",
        "continue",
        "go on",
        "keep going",
        "please continue",
    }:
        return ""
    if value.startswith("<") or value.startswith(
        (
            "# AGENTS.md",
            "# Codex",
            "You are ",
            "Side conversation boundary.",
            "This is an incremental tools update.",
        )
    ):
        return ""
    return value[:160]


def _content_text(content: Any) -> tuple[str, int, int]:
    texts: list[str] = []
    images = 0
    encrypted = 0
    if isinstance(content, str):
        return content, 0, 0
    if not isinstance(content, list):
        if isinstance(content, dict):
            text = content.get("text")
            return (text if isinstance(text, str) else ""), 0, 0
        return "", 0, 0
    for part in content:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "")
        if part_type == "encrypted_content":
            encrypted += 1
            continue
        if part_type in {"input_image", "image_url", "image", "computer_screenshot"}:
            images += 1
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
            continue
        nested = part.get("content")
        if isinstance(nested, (str, list)):
            nested_text, nested_images, nested_encrypted = _content_text(nested)
            texts.append(nested_text)
            images += nested_images
            encrypted += nested_encrypted
    return "\n".join(value for value in texts if value), images, encrypted


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def normalize_item(
    payload: dict[str, Any], *, index: int, ordinal: int | None, timestamp: str | None,
    turn_id: str | None, model: str
) -> ContextItem:
    item_type = str(payload.get("type") or "other")
    role = ""
    name = ""
    text = ""
    images = 0
    encrypted = 0

    if item_type == "message":
        role = str(payload.get("role") or "")
        text, images, encrypted = _content_text(payload.get("content"))
        kind = "message"
    elif item_type == "agent_message":
        role = "agent"
        author = payload.get("author") or "?"
        recipient = payload.get("recipient") or "?"
        name = f"{author} -> {recipient}"
        text, images, encrypted = _content_text(payload.get("content"))
        kind = "agent_message"
    elif item_type in {"custom_tool_call", "function_call"}:
        role = "assistant"
        namespace = payload.get("namespace")
        tool_name = payload.get("name") or "?"
        name = f"{namespace}.{tool_name}" if namespace else str(tool_name)
        raw = payload.get("input")
        if raw is None:
            raw = payload.get("arguments")
        text = raw if isinstance(raw, str) else _json_text(raw)
        kind = "tool_call"
    elif item_type in {"custom_tool_call_output", "function_call_output"}:
        role = "tool"
        name = str(payload.get("call_id") or "result")
        output = payload.get("output")
        if isinstance(output, str):
            text = output
        else:
            text, images, encrypted = _content_text(output)
        kind = "tool_output"
    elif item_type == "reasoning":
        role = "assistant"
        summary = payload.get("summary")
        content = payload.get("content")
        parts: list[str] = []
        for value in (summary, content):
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                chunk, chunk_images, chunk_encrypted = _content_text(value)
                parts.append(chunk)
                images += chunk_images
                encrypted += chunk_encrypted
        text = "\n".join(part for part in parts if part)
        if payload.get("encrypted_content"):
            encrypted += 1
        if not text:
            text = "[encrypted reasoning]"
        kind = "reasoning"
    elif item_type == "additional_tools":
        role = "developer"
        text = _json_text(payload.get("tools"))
        kind = "tool_schema"
    elif item_type == "configuration_update":
        role = "developer"
        text = _json_text(payload)
        kind = "config"
    else:
        role = str(payload.get("role") or "")
        text = _json_text(payload)
        kind = "other"

    est = estimate_tokens(text) + images * IMAGE_TOKEN_ESTIMATE
    if est:
        est += ITEM_OVERHEAD_TOKENS
    return ContextItem(
        index=index,
        ordinal=ordinal,
        timestamp=timestamp,
        kind=kind,
        role=role,
        name=name,
        text=text,
        chars=len(text),
        est_tokens=est,
        turn_id=turn_id,
        model=model,
        call_id=payload.get("call_id"),
        encrypted=bool(encrypted),
        image_count=images,
    )


def _compaction_item(
    payload: dict[str, Any], *, index: int, ordinal: int | None, timestamp: str | None,
    turn_id: str | None, model: str
) -> ContextItem:
    history = payload.get("replacement_history")
    replacement_tokens = 0
    count = 0
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            count += 1
            replacement_tokens += normalize_item(
                entry,
                index=index,
                ordinal=None,
                timestamp=None,
                turn_id=turn_id,
                model=model,
            ).est_tokens
    text = f"[context compacted: {count} replacement items, ~{replacement_tokens} tokens]"
    return ContextItem(
        index=index,
        ordinal=ordinal,
        timestamp=timestamp,
        kind="compaction",
        role="system",
        text=text,
        chars=len(text),
        est_tokens=replacement_tokens,
        turn_id=turn_id,
        model=model,
        replacement_items=count,
        replacement_tokens=replacement_tokens,
    )


def _dedupe_event_calls(calls: list[TokenCall]) -> list[TokenCall]:
    seen: set[tuple[Any, ...]] = set()
    result: list[TokenCall] = []
    for call in calls:
        key = (
            call.timestamp,
            call.turn_id,
            call.usage.input,
            call.usage.cached,
            call.usage.cache_write,
            call.usage.output,
            call.usage.reasoning,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(call)
    return result


def parse_rollout(
    path: Path,
    pricebook: PriceBook,
    *,
    collect_items: bool = False,
) -> RolloutSummary:
    """Parse one rollout file. Reads it once; no file under CODEX_HOME is written."""

    calls: list[TokenCall] = []
    event_calls: list[TokenCall] = []
    seen_response_ids: set[str] = set()
    turn_models: dict[str, str] = {}
    current_model = ""
    current_turn_id: str | None = None
    items: list[ContextItem] | None = [] if collect_items else None
    compaction_count = 0
    context_window: int | None = None
    thread_usage = TokenUsage()
    initial_thread_usage = TokenUsage()
    initial_call_usage = TokenUsage()
    initial_seen = False
    thread_usage_ts: str | None = None
    meta_seen = False
    first_user_message = ""
    session_id = session_id_from_path(path) or path.stem
    cwd = ""
    model_provider = ""
    thread_source = ""
    cli_version = ""

    with open_jsonl(path) as handle:
        for raw in handle:
            if not raw.strip():
                continue
            head = raw[:320]
            record_type = _record_type(head)
            if record_type == "session_meta":
                if meta_seen:
                    continue
                meta_seen = True
                meta_head = raw[:6000]
                match = META_SESSION_RE.search(meta_head)
                if match:
                    session_id = match.group(1)
                match = META_ID_RE.search(meta_head)
                if match:
                    session_id = match.group(1)
                match = META_CWD_RE.search(meta_head)
                if match:
                    cwd = match.group(1)
                match = META_PROVIDER_RE.search(meta_head)
                if match:
                    model_provider = match.group(1)
                match = META_THREAD_RE.search(meta_head)
                if match:
                    thread_source = match.group(1)
                match = META_CLI_RE.search(meta_head)
                if match:
                    cli_version = match.group(1)
                continue
            if record_type == "turn_context":
                context_head = raw[:2000]
                model_match = TURN_MODEL_RE.search(context_head)
                turn_match = TURN_ID_RE.search(context_head)
                model = model_match.group(1) if model_match else current_model
                turn_id = turn_match.group(1) if turn_match else current_turn_id
                current_model = model or current_model
                current_turn_id = turn_id
                if turn_id:
                    turn_models[turn_id] = model or current_model
                continue
            if record_type == "token_usage_record":
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") or {}
                usage_payload = payload.get("usage")
                if not isinstance(usage_payload, dict):
                    continue
                response_id = str(payload.get("response_id") or "")
                if response_id and response_id in seen_response_ids:
                    continue
                if response_id:
                    seen_response_ids.add(response_id)
                turn_id = str(payload.get("turn_id") or current_turn_id or "") or None
                model = turn_models.get(turn_id or "", current_model) or "unknown"
                thread_payload = payload.get("thread_token_usage")
                if isinstance(thread_payload, dict):
                    candidate = usage_from_dict(thread_payload)
                    call_usage = usage_from_dict(usage_payload)
                    if not initial_seen:
                        initial_thread_usage = candidate
                        initial_call_usage = call_usage
                        initial_seen = True
                    if candidate.total >= thread_usage.total:
                        thread_usage = candidate
                        thread_usage_ts = record.get("timestamp")
                calls.append(
                    TokenCall(
                        seq=len(calls),
                        timestamp=record.get("timestamp"),
                        turn_id=turn_id,
                        model=model,
                        usage=call_usage,
                        response_id=response_id,
                        context_window=context_window,
                        source="token_usage_record",
                    )
                )
                continue
            if record_type == "event_msg":
                if '"token_count"' not in head:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                window = info.get("model_context_window")
                if isinstance(window, int) and window > 0:
                    context_window = window
                usage_payload = info.get("last_token_usage")
                if not isinstance(usage_payload, dict):
                    continue
                total_payload = info.get("total_token_usage")
                if isinstance(total_payload, dict):
                    candidate = usage_from_dict(total_payload)
                    call_usage = usage_from_dict(usage_payload)
                    if not initial_seen:
                        initial_thread_usage = candidate
                        initial_call_usage = call_usage
                        initial_seen = True
                    if candidate.total >= thread_usage.total:
                        thread_usage = candidate
                        thread_usage_ts = record.get("timestamp")
                turn_id = str(payload.get("turn_id") or current_turn_id or "") or None
                model = turn_models.get(turn_id or "", current_model) or "unknown"
                event_calls.append(
                    TokenCall(
                        seq=len(event_calls),
                        timestamp=record.get("timestamp"),
                        turn_id=turn_id,
                        model=model,
                        usage=usage_from_dict(usage_payload),
                        context_window=context_window,
                        source="event_token_count",
                    )
                )
                continue
            if record_type == "compacted":
                compaction_count += 1
                if items is not None:
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    items.append(
                        _compaction_item(
                            record.get("payload") or {},
                            index=len(items),
                            ordinal=record.get("ordinal"),
                            timestamp=record.get("timestamp"),
                            turn_id=current_turn_id,
                            model=current_model,
                        )
                    )
                continue
            if record_type == "response_item":
                if items is None:
                    if first_user_message or (
                        '"role":"user"' not in head and '"role": "user"' not in head
                    ):
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    payload = record.get("payload") or {}
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text, _, _ = _content_text(payload.get("content"))
                        first_user_message = _headline(text)
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") or {}
                if not first_user_message and payload.get("type") == "message" and payload.get("role") == "user":
                    text, _, _ = _content_text(payload.get("content"))
                    first_user_message = _headline(text)
                metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                turn_id = metadata.get("turn_id") or current_turn_id
                model = turn_models.get(turn_id or "", current_model)
                items.append(
                    normalize_item(
                        payload,
                        index=len(items),
                        ordinal=record.get("ordinal"),
                        timestamp=record.get("timestamp"),
                        turn_id=turn_id,
                        model=model,
                    )
                )

    if not calls:
        calls = _dedupe_event_calls(event_calls)
    retained_total = TokenUsage()
    for call in calls:
        retained_total.add(call.usage)
    raw_lifetime = thread_usage
    if initial_seen:
        thread_usage = TokenUsage(
            input=max(0, raw_lifetime.input - initial_thread_usage.input + initial_call_usage.input),
            cached=max(0, raw_lifetime.cached - initial_thread_usage.cached + initial_call_usage.cached),
            cache_write=max(
                0,
                raw_lifetime.cache_write
                - initial_thread_usage.cache_write
                + initial_call_usage.cache_write,
            ),
            output=max(0, raw_lifetime.output - initial_thread_usage.output + initial_call_usage.output),
            reasoning=max(
                0,
                raw_lifetime.reasoning
                - initial_thread_usage.reasoning
                + initial_call_usage.reasoning,
            ),
        )
    if thread_usage.total < retained_total.total:
        thread_usage = retained_total
    timestamps = [call.timestamp for call in calls if call.timestamp]
    summary = RolloutSummary(
        session_id=session_id,
        path=path,
        cwd=cwd,
        session_model=_dominant_model(calls),
        model_provider=model_provider,
        thread_source=thread_source,
        cli_version=cli_version,
        context_window=context_window,
        compaction_count=compaction_count,
        calls=calls,
        models={},
        items=items,
        thread_usage=thread_usage,
        raw_thread_usage=raw_lifetime,
        initial_thread_usage=initial_thread_usage,
        initial_call_usage=initial_call_usage,
        thread_usage_ts=thread_usage_ts,
        paths=[path],
        first_user_message=first_user_message,
        first_ts=min(timestamps) if timestamps else None,
        last_ts=max(timestamps) if timestamps else None,
    )
    return finalize_summary(summary, pricebook)


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


def _dominant_model(calls: list[TokenCall], fallback: str = "") -> str:
    totals: dict[str, int] = {}
    for call in calls:
        totals[call.model] = totals.get(call.model, 0) + call.usage.total
    if not totals:
        return fallback or "unknown"
    model = max(totals, key=lambda key: totals[key])
    if model == "unknown":
        model = next((key for key in totals if key != "unknown"), fallback or "unknown")
    return model


def finalize_summary(summary: RolloutSummary, pricebook: PriceBook) -> RolloutSummary:
    """Cost retained calls, then estimate the lifetime remainder from thread_token_usage."""

    calls = summary.calls
    for index, call in enumerate(calls):
        call.seq = index
        if call.context_window is None:
            call.context_window = summary.context_window
    primary = _dominant_model(calls, summary.session_model)
    for call in calls:
        if call.model == "unknown" and primary != "unknown":
            call.model = primary

    models: dict[str, ModelUsage] = {}
    for call in calls:
        call.cost = pricebook.cost(
            {
                "input": call.usage.input,
                "cached": call.usage.cached,
                "cache_write": call.usage.cache_write,
                "output": call.usage.output,
            },
            call.model,
            call.timestamp,
        )
        models.setdefault(call.model, ModelUsage(call.model)).add(call)

    retained = TokenUsage()
    for call in calls:
        retained.add(call.usage)
    lifetime = summary.thread_usage if summary.thread_usage.total >= retained.total else retained
    remainder = _difference(lifetime, retained)
    if remainder.total:
        model_name = primary or "unknown"
        model_usage = models.setdefault(model_name, ModelUsage(model_name))
        cost = pricebook.cost(
            {
                "input": remainder.input,
                "cached": remainder.cached,
                "cache_write": remainder.cache_write,
                "output": remainder.output,
            },
            model_name,
            summary.thread_usage_ts or summary.last_ts,
        )
        model_usage.usage.add(remainder)
        model_usage.cost_usd += cost.usd or 0.0
        model_usage.priced_tokens += cost.priced_tokens
        model_usage.unpriced_tokens += cost.unpriced_tokens
        model_usage.estimated_tokens += remainder.total

    summary.models = models
    summary.session_model = primary
    summary.thread_usage = lifetime
    return summary


def merge_summaries(summaries: list[RolloutSummary], pricebook: PriceBook) -> RolloutSummary:
    """Merge continuation segments that share a session id into one logical session."""

    if not summaries:
        raise ValueError("no rollout segments to merge")
    if len(summaries) == 1:
        return summaries[0]
    summaries = sorted(summaries, key=lambda item: (item.first_ts or "", str(item.path)))
    first = summaries[0]
    merged = RolloutSummary(
        session_id=first.session_id,
        path=first.path,
        cwd=first.cwd,
        session_model=first.session_model,
        model_provider=first.model_provider,
        thread_source=first.thread_source,
        cli_version=first.cli_version,
        context_window=max(
            (summary.context_window or 0 for summary in summaries), default=0
        ) or None,
        compaction_count=sum(summary.compaction_count for summary in summaries),
        calls=[],
        models={},
        items=[] if any(summary.items is not None for summary in summaries) else None,
        thread_usage=TokenUsage(),
        raw_thread_usage=TokenUsage(),
        initial_thread_usage=TokenUsage(),
        initial_call_usage=TokenUsage(),
        thread_usage_ts=None,
        paths=[path for summary in summaries for path in summary.paths],
        first_user_message=next(
            (summary.first_user_message for summary in summaries if summary.first_user_message),
            "",
        ),
    )
    seen_calls: set[tuple[Any, ...]] = set()
    for summary in summaries:
        merged.thread_usage.add(summary.thread_usage)
        if summary.raw_thread_usage.total >= merged.raw_thread_usage.total:
            merged.raw_thread_usage = summary.raw_thread_usage
            merged.initial_thread_usage = summary.initial_thread_usage
            merged.initial_call_usage = summary.initial_call_usage
            merged.thread_usage_ts = summary.thread_usage_ts
        for call in summary.calls:
            key = (
                call.response_id,
                call.timestamp,
                call.turn_id,
                call.usage.input,
                call.usage.cached,
                call.usage.output,
            )
            if key in seen_calls:
                continue
            seen_calls.add(key)
            merged.calls.append(call)
        if merged.items is not None and summary.items:
            merged.items.extend(summary.items)
    merged.calls.sort(key=lambda call: (call.timestamp or "", call.seq))
    if merged.items is not None:
        merged.items.sort(
            key=lambda item: (item.timestamp or "", item.ordinal if item.ordinal is not None else item.index)
        )
        for index, item in enumerate(merged.items):
            item.index = index
    timestamps = [call.timestamp for call in merged.calls if call.timestamp]
    merged.first_ts = min(timestamps) if timestamps else None
    merged.last_ts = max(timestamps) if timestamps else None
    return finalize_summary(merged, pricebook)
