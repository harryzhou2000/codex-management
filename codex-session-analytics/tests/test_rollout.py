import json
import tempfile
import unittest
from pathlib import Path

from codex_session_analytics.pricing import PriceBook
from codex_session_analytics.rollout import estimate_tokens, merge_summaries, parse_rollout

SESSION_ID = "01a1cad8-ebac-72a2-ae28-56568c667a52"


def write_rollout(
    path: Path,
    *,
    lifetime_input: int,
    call_input: int,
    ordinal: int = 0,
    thread_id: str = SESSION_ID,
    owner_id: str = SESSION_ID,
    second_lifetime: int | None = None,
    second_call: int | None = None,
) -> None:
    records = [
        {
            "timestamp": "2026-11-11T02:00:00Z",
            "ordinal": ordinal,
            "type": "session_meta",
            "payload": {
                "session_id": owner_id,
                "id": thread_id,
                "cwd": "/tmp/project",
                "thread_source": "user",
                "model_provider": "openai",
                "cli_version": "0.167.1",
            },
        },
        {
            "timestamp": "2026-11-11T02:00:01Z",
            "ordinal": ordinal + 1,
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-6.1-sol"},
        },
        {
            "timestamp": "2026-11-11T02:00:02Z",
            "ordinal": ordinal + 2,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello world"}],
            },
        },
        {
            "timestamp": "2026-11-11T02:00:03Z",
            "ordinal": ordinal + 3,
            "type": "token_usage_record",
            "payload": {
                "response_id": f"response-{ordinal}",
                "turn_id": "turn-1",
                "usage": {
                    "input_tokens": call_input,
                    "cached_input_tokens": call_input // 2,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 1000,
                    "reasoning_output_tokens": 100,
                },
                "thread_token_usage": {
                    "input_tokens": lifetime_input,
                    "cached_input_tokens": lifetime_input // 2,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 10_000,
                    "reasoning_output_tokens": 1000,
                },
            },
        },
        {
            "timestamp": "2026-11-11T02:00:04Z",
            "ordinal": ordinal + 4,
            "type": "compacted",
            "payload": {
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "summary of earlier work"}],
                    }
                ]
            },
        },
    ]
    if second_lifetime is not None:
        records.insert(
            -1,
            {
                "timestamp": "2026-11-11T02:00:03.500Z",
                "ordinal": ordinal + 3,
                "type": "token_usage_record",
                "payload": {
                    "response_id": f"response-{ordinal}-b",
                    "turn_id": "turn-1",
                    "usage": {
                        "input_tokens": second_call or 0,
                        "cached_input_tokens": (second_call or 0) // 2,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 1000,
                        "reasoning_output_tokens": 100,
                    },
                    "thread_token_usage": {
                        "input_tokens": second_lifetime,
                        "cached_input_tokens": second_lifetime // 2,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 10_000,
                        "reasoning_output_tokens": 1000,
                    },
                },
            },
        )
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.book = PriceBook.load()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_lifetime_remainder_and_context_items(self):
        path = self.root / f"rollout-2026-11-11T02-00-00-{SESSION_ID}.jsonl"
        write_rollout(
            path,
            lifetime_input=200_000,
            call_input=100_000,
            second_lifetime=500_000,
            second_call=100_000,
        )
        summary = parse_rollout(path, self.book, collect_items=True)
        self.assertEqual(len(summary.calls), 2)
        self.assertEqual(summary.retained_usage.total, 202_000)
        self.assertEqual(summary.thread_usage.total, 401_000)
        self.assertEqual(summary.estimated_tokens, 199_000)
        self.assertGreater(summary.cost_usd, 0)
        model = next(iter(summary.models.values()))
        self.assertEqual(model.usage.total, summary.thread_usage.total)
        self.assertEqual(model.retained_usage.total, summary.retained_usage.total)
        self.assertEqual(model.estimated_tokens, summary.estimated_tokens)
        self.assertEqual(summary.compaction_count, 1)
        self.assertIsNotNone(summary.items)
        self.assertEqual([item.kind for item in summary.items], ["message", "compaction"])
        self.assertGreater(summary.items[1].replacement_tokens, 0)

    def test_segment_baseline_is_subtracted(self):
        first = self.root / f"rollout-2026-11-11T02-00-00-{SESSION_ID}.jsonl"
        second = self.root / f"rollout-2026-11-11T03-00-00-{SESSION_ID}.jsonl"
        write_rollout(first, lifetime_input=500_000, call_input=100_000, ordinal=0)
        write_rollout(second, lifetime_input=900_000, call_input=200_000, ordinal=100)
        one = parse_rollout(first, self.book, collect_items=True)
        two = parse_rollout(second, self.book, collect_items=True)
        merged = merge_summaries([one, two], self.book)
        self.assertEqual(len(merged.calls), 2)
        self.assertEqual(merged.retained_usage.total, 302_000)
        self.assertEqual(merged.thread_usage.total, 302_000)
        self.assertEqual(merged.estimated_tokens, 0)

    def test_estimate_tokens_handles_cjk(self):
        self.assertGreater(estimate_tokens("中文文本"), estimate_tokens("abc"))

    def test_subagent_uses_id_not_root_session_id(self):
        own_id = "019fd884-65a1-78d3-a0e9-7bbad2126718"
        path = self.root / f"rollout-2026-11-11T02-00-00-{own_id}.jsonl"
        write_rollout(
            path,
            lifetime_input=100_000,
            call_input=100_000,
            thread_id=own_id,
            owner_id=SESSION_ID,
        )
        summary = parse_rollout(path, self.book)
        self.assertEqual(summary.session_id, own_id)
        self.assertNotEqual(summary.session_id, SESSION_ID)

    def test_inherited_counter_baseline_is_subtracted(self):
        path = self.root / f"rollout-2026-11-11T02-00-00-{SESSION_ID}.jsonl"
        write_rollout(path, lifetime_input=500_000, call_input=100_000)
        summary = parse_rollout(path, self.book)
        self.assertEqual(summary.raw_thread_usage.total, 510_000)
        self.assertEqual(summary.thread_usage.total, 101_000)
        self.assertEqual(summary.retained_usage.total, 101_000)


if __name__ == "__main__":
    unittest.main()
