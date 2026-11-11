import unittest
from pathlib import Path

from codex_session_analytics.context import build_payload
from codex_session_analytics.rollout import (
    ContextItem,
    RolloutSummary,
    TokenCall,
    TokenUsage,
)


class ContextSliceTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            ContextItem(
                index=0,
                ordinal=0,
                timestamp="2026-11-11T01:00:00Z",
                kind="message",
                role="user",
                text="one",
                est_tokens=100,
                turn_id="t1",
            ),
            ContextItem(
                index=1,
                ordinal=1,
                timestamp="2026-11-11T02:00:00Z",
                kind="message",
                role="assistant",
                text="two",
                est_tokens=50,
                turn_id="t2",
            ),
            ContextItem(
                index=2,
                ordinal=2,
                timestamp="2026-11-11T03:00:00Z",
                kind="message",
                role="user",
                text="three",
                est_tokens=75,
                turn_id="t3",
            ),
        ]
        self.calls = [
            TokenCall(
                seq=0,
                timestamp="2026-11-11T01:00:00Z",
                turn_id="t1",
                model="m",
                usage=TokenUsage(input=1000, output=100),
            ),
            TokenCall(
                seq=1,
                timestamp="2026-11-11T02:00:00Z",
                turn_id="t2",
                model="m",
                usage=TokenUsage(input=2000, output=100),
            ),
            TokenCall(
                seq=2,
                timestamp="2026-11-11T03:00:00Z",
                turn_id="t3",
                model="m",
                usage=TokenUsage(input=3000, output=100),
            ),
        ]
        self.summary = RolloutSummary(
            session_id="test",
            path=Path("test.jsonl"),
            items=self.items,
            calls=self.calls,
        )

    def payload(self, **kwargs):
        return build_payload(
            self.summary,
            name="test",
            thread_source="user",
            cwd="/tmp",
            source="live",
            **kwargs,
        )

    def test_last_turns_snaps_and_filters_calls(self):
        payload = self.payload(granularity="turn", last_turns=2)
        self.assertEqual(payload["summary"]["turns"], 2)
        self.assertEqual([turn["turn_id"] for turn in payload["turns"]], ["t2", "t3"])
        self.assertEqual(len(payload["calls"]), 2)
        self.assertEqual(payload["summary"]["input_tokens"], 5000)
        self.assertEqual(payload["summary"]["filtered"], True)

    def test_last_messages_filters_items(self):
        payload = self.payload(last_messages=1)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["text"], "three")

    def test_time_window_uses_session_reference(self):
        payload = self.payload(last_hours=1.1)
        self.assertEqual([item["text"] for item in payload["items"]], ["two", "three"])
        self.assertEqual(len(payload["calls"]), 2)


if __name__ == "__main__":
    unittest.main()
