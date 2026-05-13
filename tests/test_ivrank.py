import unittest
from unittest.mock import patch

from core.ivrank import iv_rank_from_history, vix_proxy_ivr


class IVRankTests(unittest.TestCase):
    def test_rank_and_percentile_calculation(self):
        hist = [0.10 + i * 0.01 for i in range(40)]
        out = iv_rank_from_history(0.30, hist)
        self.assertEqual(out["method"], "self_collected")
        self.assertAlmostEqual(out["iv_rank"], 51.3, places=1)
        self.assertEqual(out["iv_percentile"], 50.0)

    def test_insufficient_history_handling(self):
        out = iv_rank_from_history(0.30, [0.2, 0.25])
        self.assertEqual(out["method"], "insufficient_history")
        self.assertIsNone(out["iv_rank"])
        self.assertIn("insufficient_iv_history", out["data_quality_flags"])

    @patch("core.ivrank.yf_history")
    def test_proxy_fallback(self, mock_hist):
        import pandas as pd
        mock_hist.return_value = pd.DataFrame({"Close": [12, 20, 30, 18]})
        out = vix_proxy_ivr(0.35)
        self.assertEqual(out["method"], "vix_proxy")
        self.assertIsNotNone(out["proxy_ivr"])


if __name__ == "__main__":
    unittest.main()
