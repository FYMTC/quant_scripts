import unittest
from unittest.mock import patch

from trade_account_context import normalize_portfolio_truth
from apps import morning
import risk_monitor
import risk_check
import snapshot_reader


SNAPSHOT = {
    "account_id": "paper_easyths",
    "position_source": "easyths",
    "cash": 12345.0,
    "total_value": 33345.0,
    "positions": [
        {
            "code": "000001",
            "name": "平安银行",
            "shares": 1000,
            "cost": 10.0,
            "last_price": 21.0,
            "market_value": 21000.0,
            "profit": 11000.0,
        }
    ],
}


class TestPortfolioSnapshotAdapters(unittest.TestCase):
    def test_normalize_portfolio_truth(self):
        portfolio = normalize_portfolio_truth(SNAPSHOT)
        self.assertEqual(portfolio["cash"], 12345.0)
        self.assertEqual(portfolio["total_assets"], 33345.0)
        self.assertIn("000001", portfolio["positions"])
        self.assertEqual(portfolio["positions"]["000001"]["shares"], 1000)

    @patch("apps.morning.fetch_kline_baostock")
    @patch("apps.morning.load_portfolio_truth")
    def test_morning_load_holdings_uses_snapshot(self, mock_portfolio, mock_kline):
        mock_portfolio.return_value = normalize_portfolio_truth(SNAPSHOT)
        mock_kline.return_value = [
            {"收盘": "20.0"},
            {"收盘": "21.0"},
        ]
        holdings, cash, total_assets = morning.load_holdings()
        self.assertEqual(cash, 12345.0)
        self.assertEqual(total_assets, 33345.0)
        self.assertEqual(holdings[0]["code"], "000001")
        self.assertEqual(holdings[0]["price"], 21.0)

    def test_allocate_buy_candidates_creates_multi_name_plan(self):
        candidates = [
            {"code": "300408", "name": "A", "price": 100.0, "composite_score": 1.6, "garch_vol": 50.0, "cvar": -4.0},
            {"code": "603629", "name": "B", "price": 80.0, "composite_score": 1.5, "garch_vol": 40.0, "cvar": -4.5},
            {"code": "000725", "name": "C", "price": 10.0, "composite_score": 1.2, "garch_vol": 35.0, "cvar": -3.0},
        ]
        feature_snapshot = {"per_stock": {}}
        event_risk = {"playbook": {"buy_score_threshold": 1.0, "max_gross_exposure": 0.8, "allow_new_buy": True}}
        proposals = morning.allocate_buy_candidates([], 90000.0, 100000.0, candidates, feature_snapshot, event_risk)
        self.assertGreaterEqual(len(proposals), 2)
        self.assertEqual({p["account_id"] for p in proposals}, {"paper_easyths"})
        self.assertTrue(all(p["shares"] % 100 == 0 for p in proposals))

    def test_allocate_buy_candidates_allows_single_probe_under_high_macro_risk(self):
        candidates = [
            {"code": "300408", "name": "A", "price": 25.0, "composite_score": 1.6, "garch_vol": 35.0, "cvar": -4.7},
            {"code": "600584", "name": "B", "price": 20.0, "composite_score": 1.55, "garch_vol": 30.0, "cvar": -6.0},
        ]
        feature_snapshot = {"per_stock": {"300408": {"risk_level": "medium", "cvar": -4.7}, "600584": {"risk_level": "medium", "cvar": -6.0}}}
        event_risk = {"playbook": {"level": "HIGH", "buy_score_threshold": 1.2, "max_gross_exposure": 0.5, "allow_new_buy": False}}
        proposals = morning.allocate_buy_candidates([], 90000.0, 100000.0, candidates, feature_snapshot, event_risk)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["code"], "300408")
        self.assertLessEqual(proposals[0]["buy_value"], 3000.0)
        self.assertIn("macro_probe=HIGH", proposals[0]["rationale"])

    def test_allocate_buy_candidates_still_blocks_non_high_macro_ban(self):
        candidates = [
            {"code": "300408", "name": "A", "price": 25.0, "composite_score": 1.6, "garch_vol": 35.0, "cvar": -4.7},
        ]
        feature_snapshot = {"per_stock": {"300408": {"risk_level": "medium", "cvar": -4.7}}}
        event_risk = {"playbook": {"level": "CRITICAL", "buy_score_threshold": 1.2, "max_gross_exposure": 0.5, "allow_new_buy": False}}
        proposals = morning.allocate_buy_candidates([], 90000.0, 100000.0, candidates, feature_snapshot, event_risk)
        self.assertEqual(proposals, [])

    @patch("risk_monitor.load_portfolio_truth")
    def test_risk_monitor_loads_snapshot_portfolio(self, mock_portfolio):
        normalized = normalize_portfolio_truth(SNAPSHOT)
        mock_portfolio.return_value = normalized
        self.assertEqual(risk_monitor.load_portfolio_from_db(), normalized)

    @patch("risk_check.load_portfolio_truth")
    def test_risk_check_loads_snapshot_portfolio(self, mock_portfolio):
        normalized = normalize_portfolio_truth(SNAPSHOT)
        mock_portfolio.return_value = normalized
        risk_check._portfolio_cache = None
        self.assertEqual(risk_check._get_portfolio_truth(), normalized)

    @patch("snapshot_reader.load_portfolio_truth")
    def test_snapshot_reader_loads_snapshot_positions(self, mock_portfolio):
        normalized = normalize_portfolio_truth(SNAPSHOT)
        mock_portfolio.return_value = normalized
        positions = snapshot_reader._load_positions_config()
        self.assertIn("000001", positions)
        self.assertEqual(positions["000001"]["shares"], 1000)


if __name__ == "__main__":
    unittest.main()
