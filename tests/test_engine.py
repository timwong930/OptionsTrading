import unittest
from unittest.mock import patch

from core.engine import analyze_trade


def contract(symbol, option_type, strike, mid, expiry="2026-07-17", delta=0.5, liquid=True):
    return {
        "symbol": symbol,
        "option_type": option_type,
        "strike": strike,
        "mid": mid,
        "bid": mid - 0.05,
        "ask": mid + 0.05,
        "expiry": expiry,
        "delta": delta if option_type == "call" else -delta,
        "iv": 0.3,
        "liquid": liquid,
        "stale": False,
        "data_quality_flags": [] if liquid else ["wide_spread"],
        "contractSymbol": f"{symbol}260717{'C' if option_type == 'call' else 'P'}{int(strike*1000):08d}",
    }


class StrategyEngineTests(unittest.TestCase):
    def setUp(self):
        self.tech = {"price": 100.0, "sma50": 95.0, "sma200": 90.0, "rsi14": 55.0, "support_20d": 94.0, "resistance_20d": 105.0, "avg_close_20d": 100.0, "bias": "bullish"}
        self.chain = [
            contract("XYZ", "call", 100, 4.0),
            contract("XYZ", "call", 105, 2.0),
            contract("XYZ", "call", 110, 1.0),
            contract("XYZ", "put", 95, 1.2),
            contract("XYZ", "put", 90, 0.6),
            contract("XYZ", "put", 85, 0.3),
        ]

    @patch("core.engine.yf_sector", return_value="Technology")
    @patch("core.engine.log_atm_iv")
    @patch("core.engine.iv_rank", return_value={"iv_rank": 35, "iv_percentile": 40, "method": "self_collected", "data_quality_flags": []})
    @patch("core.engine.get_option_chain")
    @patch("core.engine.catalyst_snapshot", return_value={"catalyst": True, "catalyst_summary": "test", "data_quality_flags": [], "event_risk_notes": []})
    @patch("core.engine._technical_snapshot")
    def test_picks_spread_when_budget_is_tight(self, mock_tech, _cat, mock_chain, *_):
        mock_tech.return_value = self.tech
        mock_chain.return_value = self.chain
        out = analyze_trade("XYZ", bias="bullish", budget=250, portfolio_value=10000)
        self.assertEqual(out["recommended_strategy"], "call_debit_spread")
        self.assertTrue(out["budget_fit"])
        self.assertLessEqual(out["max_loss"], 200)

    @patch("core.engine.yf_sector", return_value="Technology")
    @patch("core.engine.get_option_chain")
    @patch("core.engine.catalyst_snapshot", return_value={"catalyst": False, "catalyst_summary": "none", "data_quality_flags": [], "event_risk_notes": []})
    @patch("core.engine._technical_snapshot")
    def test_skips_poor_liquidity(self, mock_tech, _cat, mock_chain, *_):
        mock_tech.return_value = self.tech
        bad = [dict(c, liquid=False, data_quality_flags=["wide_spread"]) for c in self.chain]
        mock_chain.return_value = bad
        out = analyze_trade("XYZ", bias="bullish", budget=500, portfolio_value=10000)
        self.assertEqual(out["recommended_strategy"], "no_trade")
        self.assertEqual(out["reason"], "insufficient liquid contracts")

    @patch("core.engine.yf_sector", return_value="Technology")
    @patch("core.engine.log_atm_iv")
    @patch("core.engine.iv_rank", return_value={"iv_rank": 35, "iv_percentile": 40, "method": "self_collected", "data_quality_flags": []})
    @patch("core.engine.get_option_chain")
    @patch("core.engine.catalyst_snapshot", return_value={"catalyst": True, "catalyst_summary": "test", "data_quality_flags": [], "event_risk_notes": []})
    @patch("core.engine._technical_snapshot")
    def test_reports_budget_cap_without_clamping_to_old_default(self, mock_tech, _cat, mock_chain, *_):
        mock_tech.return_value = self.tech
        mock_chain.return_value = self.chain
        out = analyze_trade("XYZ", bias="bullish", budget=500, portfolio_value=5000)
        self.assertEqual(out["risk_budget"], 250)
        self.assertTrue(out["budget"]["capped"])
        self.assertEqual(out["budget"]["requested_budget"], 500)
        self.assertIn("Risk budget capped", out["budget"]["cap_explanation"])

    @patch("core.engine.yf_sector", return_value="Technology")
    @patch("core.engine.log_atm_iv")
    @patch("core.engine.iv_rank", return_value={"iv_rank": None, "iv_percentile": None, "method": "insufficient_history", "history_quality": "insufficient_history", "data_quality_flags": ["insufficient_iv_history"], "fallback": {"proxy_ivr": 45, "method": "vix_proxy"}})
    @patch("core.engine.get_option_chain")
    @patch("core.engine.catalyst_snapshot", return_value={"catalyst": True, "catalyst_summary": "test", "data_quality_flags": [], "event_risk_notes": []})
    @patch("core.engine._technical_snapshot")
    def test_thin_iv_history_downgrades_but_does_not_block_liquid_trade(self, mock_tech, _cat, mock_chain, *_):
        mock_tech.return_value = self.tech
        mock_chain.return_value = self.chain
        out = analyze_trade("XYZ", bias="bullish", budget=250, portfolio_value=10000)
        self.assertTrue(out["tradable"])
        self.assertEqual(out["iv_history_quality"], "insufficient_history")
        self.assertEqual(out["iv_proxy_label"], "PROXY IV ESTIMATE - not single-name IV rank")
        self.assertIn("insufficient_iv_history", out["data_quality_flags"])

    @patch("core.engine._technical_snapshot", return_value={"bias": "neutral", "price": 100})
    def test_rejects_bad_setups(self, _tech):
        out = analyze_trade("XYZ", bias="auto")
        self.assertEqual(out["recommended_strategy"], "no_trade")

    @patch("core.engine.yf_sector", return_value="Technology")
    @patch("core.engine.log_atm_iv")
    @patch("core.engine.iv_rank", return_value={"iv_rank": 35, "iv_percentile": 40, "method": "self_collected", "data_quality_flags": []})
    @patch("core.engine.get_option_chain")
    @patch("core.engine.catalyst_snapshot", return_value={"catalyst": True, "catalyst_summary": "test", "data_quality_flags": [], "event_risk_notes": []})
    @patch("core.engine._technical_snapshot")
    def test_respects_time_horizon(self, mock_tech, _cat, mock_chain, *_):
        mock_tech.return_value = self.tech
        long_chain = self.chain + [contract("XYZ", "call", 100, 5.0, expiry="2026-10-16"), contract("XYZ", "call", 105, 3.0, expiry="2026-10-16")]
        mock_chain.return_value = long_chain
        out = analyze_trade("XYZ", bias="bullish", budget=250, portfolio_value=10000, horizon_days=90)
        self.assertEqual(out["recommended_expiry"], "2026-10-16")


if __name__ == "__main__":
    unittest.main()
