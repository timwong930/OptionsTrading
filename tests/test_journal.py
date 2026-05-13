import os
import tempfile
import unittest

from core.journal import close_trade, create_trade, list_trades, trade_stats, update_trade


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "paper.sqlite")
        self.plan = {"symbol": "XYZ", "recommended_strategy": "call_debit_spread", "suggested_contract_count": 1, "estimated_debit": 1.5, "max_loss": 150, "catalyst_summary": "test"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_update_close_and_stats(self):
        trade = create_trade(self.plan, db_path=self.db)
        self.assertEqual(trade["status"], "active")
        updated = update_trade(trade["id"], db_path=self.db, thesis="updated")
        self.assertEqual(updated["thesis"], "updated")
        active = list_trades(status="active", db_path=self.db)
        self.assertEqual(len(active), 1)
        closed = close_trade(trade["id"], 2.5, lessons="worked", db_path=self.db)
        self.assertEqual(closed["status"], "closed")
        stats = trade_stats(db_path=self.db)
        self.assertEqual(stats["closed_trades"], 1)
        self.assertEqual(stats["total_pnl"], 100.0)


if __name__ == "__main__":
    unittest.main()
