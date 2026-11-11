import unittest

from codex_session_analytics.economics import estimate, parse_lengths
from codex_session_analytics.pricing import PriceBook


class EconomicsTests(unittest.TestCase):
    def test_parse_lengths(self):
        self.assertEqual(parse_lengths("64k,0.5m,380000"), [64_000, 380_000, 500_000])

    def test_amortized_cycle_uses_summary_and_turn_lengths(self):
        call = {
            "model": "gpt-6.1-sol",
            "timestamp": "2026-11-11T12:00:00Z",
            "input_tokens": 1_000_000,
            "cached_input_tokens": 500_000,
            "cache_write_input_tokens": 0,
            "output_tokens": 10_000,
            "cost_usd": 1.75,
        }
        calls = [call, dict(call)]
        items = [
            {"kind": "message", "est_tokens": 20_000},
            {"kind": "message", "est_tokens": 30_000},
            {"kind": "compaction", "replacement_tokens": 10_000},
        ]
        result = estimate(
            calls, items, PriceBook.load(), [20_000, 100_000, 380_000], fixed_calls=10
        )
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["fixed_calls"], 10)
        self.assertEqual(result["summary_tokens"], 10_000)
        self.assertEqual(result["growth_per_call"], 25_000)
        self.assertAlmostEqual(result["actual_with_surcharge_usd"], 3.5)
        self.assertAlmostEqual(result["actual_no_surcharge_usd"], 2.3)
        self.assertEqual(result["rows"][0]["estimated_compactions"], 10)
        self.assertEqual(result["rows"][-1]["current"], True)
        self.assertLess(result["rows"][0]["cost_usd"], result["rows"][1]["cost_usd"])


if __name__ == "__main__":
    unittest.main()
