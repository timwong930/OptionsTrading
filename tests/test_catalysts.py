import datetime as dt
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import core.catalysts as cat


class CatalystTests(unittest.TestCase):
    def test_earnings_extraction(self):
        idx = pd.to_datetime(["2026-04-30", "2026-08-01"])
        df = pd.DataFrame({"Reported EPS": [1.1, None], "EPS Estimate": [1.0, 1.2]}, index=idx)
        t = Mock()
        t.get_earnings_dates.return_value = df
        out = cat._earnings_dates(t)
        self.assertEqual(out["next_earnings_date"], "2026-08-01")
        self.assertEqual(out["recent_earnings"]["eps_surprise_pct"], 10.0)

    def test_news_summarization_and_analyst_parse(self):
        snap = {"earnings_in_days": 20, "recent_earnings": {"eps_surprise_pct": 5}, "headlines": [{"title": "Beat"}], "analyst_actions": [{"firm": "X", "action": "up"}], "sector_tailwind": {"active": False}}
        summary = cat.summarize_catalyst(snap)
        self.assertIn("earnings in 20 days", summary)
        self.assertIn("recent EPS surprise +5.0%", summary)
        self.assertIn("latest analyst action up", summary)

    @patch("core.catalysts.yf.Ticker")
    @patch("core.catalysts.get_sector_tailwind", return_value={"active": False})
    def test_graceful_fallback_behavior(self, _tailwind, mock_ticker):
        t = Mock()
        t.get_earnings_dates.side_effect = RuntimeError("boom")
        type(t).news = []
        mock_ticker.return_value = t
        out = cat.catalyst_snapshot.__wrapped__("MSFT", 45, None)
        self.assertIn("earnings_lookup_failed:RuntimeError", out["data_quality_flags"])
        self.assertIn("catalyst_summary", out)


if __name__ == "__main__":
    unittest.main()
