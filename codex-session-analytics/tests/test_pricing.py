import unittest

from codex_session_analytics.pricing import PriceBook


class PriceBookTests(unittest.TestCase):
    def setUp(self):
        self.book = PriceBook.load()

    def test_cached_and_output_formula(self):
        cost = self.book.cost(
            {"input": 100_000, "cached": 50_000, "cache_write": 0, "output": 10_000},
            "gpt-6.1-sol",
        )
        self.assertAlmostEqual(cost.usd, 0.205, places=6)
        self.assertEqual(cost.priced_tokens, 110_000)
        self.assertEqual(cost.unpriced_tokens, 0)

    def test_long_context_multipliers(self):
        cost = self.book.cost(
            {"input": 300_000, "cached": 100_000, "cache_write": 0, "output": 10_000},
            "gpt-6.1-sol",
        )
        self.assertAlmostEqual(cost.usd, 0.97, places=6)

    def test_deepseek_peak_and_off_peak(self):
        peak = self.book.cost(
            {"input": 1_000_000, "cached": 500_000, "cache_write": 0, "output": 100_000},
            "deepseek/deepseek-flash",
            "2026-11-11T02:00:00Z",
        )
        off_peak = self.book.cost(
            {"input": 1_000_000, "cached": 500_000, "cache_write": 0, "output": 100_000},
            "deepseek/deepseek-flash",
            "2026-11-11T12:00:00Z",
        )
        self.assertAlmostEqual(peak.usd, 0.273, places=6)
        self.assertAlmostEqual(off_peak.usd, 0.1365, places=6)

    def test_unknown_and_local_models(self):
        unknown = self.book.cost({"input": 1000, "output": 100}, "unknown/model")
        local = self.book.cost({"input": 1000, "output": 100}, "ollama/foo")
        self.assertIsNone(unknown.usd)
        self.assertEqual(unknown.unpriced_tokens, 1100)
        self.assertEqual(local.usd, 0.0)


if __name__ == "__main__":
    unittest.main()
