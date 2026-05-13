import datetime as dt
import unittest

from core.chains import normalize_option_row, parse_occ_contract


class ChainNormalizationTests(unittest.TestCase):
    def test_occ_strike_parsing(self):
        parsed = parse_occ_contract("AAPL260717C00110000")
        self.assertFalse(parsed["malformed_contract"])
        self.assertEqual(parsed["expiry_from_contract"], "2026-07-17")
        self.assertEqual(parsed["type_from_contract"], "call")
        self.assertEqual(parsed["strike_from_contract"], 110.0)

    def test_malformed_contract_handling(self):
        row = {"contractSymbol": "BAD", "strike": 110, "bid": 1, "ask": 1.2, "volume": 20, "openInterest": 200}
        out = normalize_option_row(row, "AAPL", "2026-07-17", "call", spot=100, today=dt.date(2026, 5, 13))
        self.assertIn("malformed_contract", out["data_quality_flags"])
        self.assertTrue(out["malformed_contract"])

    def test_spread_calculations(self):
        row = {"contractSymbol": "AAPL260717C00110000", "strike": 110, "bid": 1.0, "ask": 1.2, "volume": 20, "openInterest": 200, "lastTradeDate": dt.date(2026, 5, 13)}
        out = normalize_option_row(row, "AAPL", "2026-07-17", "call", spot=100, today=dt.date(2026, 5, 13))
        self.assertEqual(out["mid"], 1.1)
        self.assertEqual(out["spread"], 0.2)
        self.assertAlmostEqual(out["spread_pct"], 18.18)

    def test_stale_contract_detection_and_liquidity_flags(self):
        row = {"contractSymbol": "AAPL260717C00110000", "strike": 110, "bid": 0.1, "ask": 0.5, "volume": 0, "openInterest": 10, "lastTradeDate": dt.date(2026, 5, 1)}
        out = normalize_option_row(row, "AAPL", "2026-07-17", "call", spot=100, today=dt.date(2026, 5, 13))
        self.assertTrue(out["stale"])
        self.assertFalse(out["liquid"])
        self.assertIn("stale_contract", out["data_quality_flags"])
        self.assertIn("low_open_interest", out["data_quality_flags"])
        self.assertIn("wide_spread", out["data_quality_flags"])

    def test_strike_scaling_sanity_check(self):
        row = {"contractSymbol": "AAPL260717C00110000", "strike": 110000, "bid": 1, "ask": 1.1, "volume": 20, "openInterest": 200, "lastTradeDate": dt.date(2026, 5, 13)}
        out = normalize_option_row(row, "AAPL", "2026-07-17", "call", spot=100, today=dt.date(2026, 5, 13))
        self.assertEqual(out["strike"], 110.0)
        self.assertIn("strike_scaled_from_raw", out["data_quality_flags"])


if __name__ == "__main__":
    unittest.main()
