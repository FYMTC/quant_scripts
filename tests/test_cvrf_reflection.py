"""T1.10 三期 cvrf_reflection 单元测试（2026-07-04）

测试覆盖：
  - cfg 导入顺序修复（模块可正常导入，不抛 NameError）
  - _normalize_rationale: 数值剥离 + 关键词聚类
  - _cluster_by_rationale: 同模式归并
  - _evaluate_event_outcome: stock_trades 交叉引用 + baostock 兜底
  - run_weekly_gc: 胜率 > 60% 收紧 / < 40% 移除 / 40-60% 保持 / 空数据 insufficient_data
  - CLI --mode weekly-gc / --mode nightly 入口

隔离：每个测试用 tempfile + patch DB_PATH + patch cfg.path.cvrf_weekly_gc，
     不污染真实 trade_log.db / cvrf_weekly_gc.json。
"""

import unittest
import sys
import os
import json
import tempfile
import sqlite3
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")
os.chdir("/root/ai_trading_package/quant/quant_scripts")


def _make_isolated_db():
    """创建临时 DB 并返回 (tmp_path)。
    用 TradeDB 初始化完整 schema（含 trading_journal 扩展列 + stock_trades）。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    import trade_db
    db = trade_db.TradeDB.__new__(trade_db.TradeDB)
    db.db_path = tmp.name
    db._ensure_db()
    # 补建 stock_trades 表（TradeDB._ensure_db 不建它，由 stock_kb 建）
    conn = sqlite3.connect(tmp.name)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT, trade_date TEXT, action TEXT,
            price REAL, shares INTEGER, amount REAL,
            pnl REAL, pnl_pct REAL, holding_days INTEGER,
            rationale TEXT, decision_process TEXT, signal_source TEXT,
            was_good_decision INTEGER, lessons TEXT,
            market_condition TEXT, market_index_level TEXT,
            created_at TEXT, account_id TEXT
        )
    """)
    conn.commit()
    conn.close()
    return tmp.name


class TestCfgImportFix(unittest.TestCase):
    """测试 T1.10 三期 cfg 导入顺序修复。"""

    def test_module_imports_without_nameerror(self):
        """模块可正常导入，不抛 NameError。"""
        import cvrf_reflection
        self.assertTrue(hasattr(cvrf_reflection, "cfg"))
        self.assertTrue(hasattr(cvrf_reflection, "DB_PATH"))
        self.assertTrue(cvrf_reflection.DB_PATH.endswith("trade_log.db"))

    def test_cfg_root_accessible(self):
        """cfg.root 在模块加载时即可用。"""
        import cvrf_reflection
        self.assertTrue(cvrf_reflection.cfg.root.startswith("/root/"))


class TestNormalizeRationale(unittest.TestCase):
    """测试 _normalize_rationale 数值剥离 + 关键词提取。"""

    def test_strips_numeric_values(self):
        from cvrf_reflection import _normalize_rationale
        result = _normalize_rationale("急跌-3.23%+cp=0.32+close>MA20+股吧情绪偏空=抄底机会")
        self.assertEqual(result, "急跌|cp|close>MA|股吧情绪偏空")

    def test_same_pattern_clusters_together(self):
        """不同数值的同模式应归一。"""
        from cvrf_reflection import _normalize_rationale
        r1 = _normalize_rationale("急跌-3.23%+cp=0.32+close>MA20")
        r2 = _normalize_rationale("急跌-2.5%+cp=0.3+close>MA20")
        self.assertEqual(r1, r2)
        self.assertEqual(r1, "急跌|cp|close>MA")

    def test_preserves_comparison_direction(self):
        """< > 比较符应保留（方向语义重要）。"""
        from cvrf_reflection import _normalize_rationale
        self.assertEqual(_normalize_rationale("cp<0.4"), "cp<")
        self.assertEqual(_normalize_rationale("cp>0.6"), "cp>")

    def test_empty_string(self):
        from cvrf_reflection import _normalize_rationale
        self.assertEqual(_normalize_rationale(""), "")

    def test_pure_numeric_returns_empty(self):
        from cvrf_reflection import _normalize_rationale
        self.assertEqual(_normalize_rationale("3.14"), "")

    def test_max_four_keywords(self):
        from cvrf_reflection import _normalize_rationale
        result = _normalize_rationale("a=1+b=2+c=3+d=4+e=5+f=6")
        self.assertEqual(len(result.split("|")), 4)


class TestClusterByRationale(unittest.TestCase):
    """测试 _cluster_by_rationale 聚类。"""

    def test_groups_same_pattern(self):
        from cvrf_reflection import _cluster_by_rationale
        events = [
            {"rationale": "急跌-3.23%+cp=0.32+close>MA20", "code": "002049"},
            {"rationale": "急跌-2.5%+cp=0.3+close>MA20", "code": "000938"},
            {"rationale": "急跌-1.8%+cp=0.28+close>MA20", "code": "600487"},
        ]
        clusters = _cluster_by_rationale(events)
        self.assertEqual(len(clusters), 1)
        pattern = list(clusters.keys())[0]
        self.assertEqual(pattern, "急跌|cp|close>MA")
        self.assertEqual(len(clusters[pattern]), 3)

    def test_separates_different_patterns(self):
        from cvrf_reflection import _cluster_by_rationale
        events = [
            {"rationale": "急跌-3.23%+cp<0.4", "code": "002049"},
            {"rationale": "rolling_decline+止损未触发", "code": "000063"},
        ]
        clusters = _cluster_by_rationale(events)
        self.assertEqual(len(clusters), 2)

    def test_empty_rationale_goes_to_uncategorized(self):
        from cvrf_reflection import _cluster_by_rationale
        events = [{"rationale": "", "code": "002049"}]
        clusters = _cluster_by_rationale(events)
        self.assertIn("_uncategorized_", clusters)


class TestEvaluateEventOutcome(unittest.TestCase):
    """测试 _evaluate_event_outcome 胜率评估。"""

    def test_hold_wait_returns_neutral(self):
        """HOLD/WAIT/T_FLIP 无法客观评估，返回 neutral。"""
        from cvrf_reflection import _evaluate_event_outcome
        for action in ("HOLD", "WAIT", "T_FLIP", ""):
            ev = {"code": "002049", "date": "2026-06-29", "action": action}
            self.assertEqual(_evaluate_event_outcome(ev), "neutral")

    def test_missing_code_or_date_returns_neutral(self):
        from cvrf_reflection import _evaluate_event_outcome
        self.assertEqual(_evaluate_event_outcome({"action": "BUY"}), "neutral")
        self.assertEqual(_evaluate_event_outcome({"code": "002049"}), "neutral")

    def test_stock_trades_with_pnl_win(self):
        """stock_trades 有匹配成交 + pnl > 0 → win。"""
        from cvrf_reflection import _evaluate_event_outcome
        tmp_db = _make_isolated_db()
        # 插入一条 pnl>0 的成交
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO stock_trades (stock_code, trade_date, action, pnl) VALUES (?, ?, ?, ?)",
            ["002049", "2026-06-29", "SELL", 1200.0]
        )
        conn.commit()
        conn.close()
        ev = {"code": "002049", "date": "2026-06-29", "action": "SELL"}
        with patch("cvrf_reflection.DB_PATH", tmp_db):
            result = _evaluate_event_outcome(ev)
        self.assertEqual(result, "win")
        os.unlink(tmp_db)

    def test_stock_trades_with_pnl_loss(self):
        """stock_trades 有匹配成交 + pnl < 0 → loss。"""
        from cvrf_reflection import _evaluate_event_outcome
        tmp_db = _make_isolated_db()
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO stock_trades (stock_code, trade_date, action, pnl) VALUES (?, ?, ?, ?)",
            ["002049", "2026-06-29", "SELL", -500.0]
        )
        conn.commit()
        conn.close()
        ev = {"code": "002049", "date": "2026-06-29", "action": "SELL"}
        with patch("cvrf_reflection.DB_PATH", tmp_db):
            result = _evaluate_event_outcome(ev)
        self.assertEqual(result, "loss")
        os.unlink(tmp_db)

    def test_baostock_fallback_buy_up_win(self):
        """无成交 → baostock 兜底：BUY + 价格上涨 → win。"""
        from cvrf_reflection import _evaluate_event_outcome
        tmp_db = _make_isolated_db()
        ev = {"code": "002049", "date": "2026-06-29", "action": "BUY"}
        with patch("cvrf_reflection.DB_PATH", tmp_db), \
             patch("cvrf_reflection._fetch_post_event_price") as mock_price:
            mock_price.return_value = (10.0, 10.5, 5.0)  # 涨 5%
            self.assertEqual(_evaluate_event_outcome(ev), "win")
        os.unlink(tmp_db)

    def test_baostock_fallback_buy_down_loss(self):
        """无成交 → baostock 兜底：BUY + 价格下跌 → loss。"""
        from cvrf_reflection import _evaluate_event_outcome
        tmp_db = _make_isolated_db()
        ev = {"code": "002049", "date": "2026-06-29", "action": "BUY"}
        with patch("cvrf_reflection.DB_PATH", tmp_db), \
             patch("cvrf_reflection._fetch_post_event_price") as mock_price:
            mock_price.return_value = (10.0, 9.5, -5.0)
            self.assertEqual(_evaluate_event_outcome(ev), "loss")
        os.unlink(tmp_db)

    def test_baostock_fallback_sell_down_win(self):
        """SELL + 卖后价格下跌 → win（卖在高位）。"""
        from cvrf_reflection import _evaluate_event_outcome
        tmp_db = _make_isolated_db()
        ev = {"code": "002049", "date": "2026-06-29", "action": "SELL"}
        with patch("cvrf_reflection.DB_PATH", tmp_db), \
             patch("cvrf_reflection._fetch_post_event_price") as mock_price:
            mock_price.return_value = (10.0, 9.5, -5.0)
            self.assertEqual(_evaluate_event_outcome(ev), "win")
        os.unlink(tmp_db)

    def test_baostock_none_returns_neutral(self):
        """baostock 失败返回 None → neutral。"""
        from cvrf_reflection import _evaluate_event_outcome
        tmp_db = _make_isolated_db()
        ev = {"code": "002049", "date": "2026-06-29", "action": "BUY"}
        with patch("cvrf_reflection.DB_PATH", tmp_db), \
             patch("cvrf_reflection._fetch_post_event_price") as mock_price:
            mock_price.return_value = None
            self.assertEqual(_evaluate_event_outcome(ev), "neutral")
        os.unlink(tmp_db)


class TestRunWeeklyGc(unittest.TestCase):
    """测试 run_weekly_gc 主流程。"""

    def _setup_isolated_gc(self):
        """创建隔离的 GC 测试环境：临时 DB + 临时输出路径。"""
        tmp_db = _make_isolated_db()
        fd, tmp_out = tempfile.mkstemp(suffix=".json", prefix="cvrf_gc_test_")
        os.close(fd)
        os.unlink(tmp_out)  # 让 _save_gc_report 自己创建
        return tmp_db, tmp_out

    def _insert_decision_events(self, db_path, events):
        """直接插入决策事件到 trading_journal。"""
        conn = sqlite3.connect(db_path)
        for ev in events:
            conn.execute(
                """INSERT INTO trading_journal
                   (time, date, type, code, name, action, rationale)
                   VALUES (?, ?, '决策事件', ?, ?, ?, ?)""",
                [ev.get("time", "10:00:00"), ev["date"], ev["code"],
                 ev.get("name", ""), ev["action"], ev.get("rationale", "")]
            )
        conn.commit()
        conn.close()

    def _patch_cfg_path(self, tmp_out):
        """patch cvrf_reflection.cfg 为带 cvrf_weekly_gc=tmp_out 的 mock。"""
        import cvrf_reflection
        mock_cfg = MagicMock()
        mock_cfg.path.cvrf_weekly_gc = tmp_out
        mock_cfg.root = cvrf_reflection.cfg.root
        return patch("cvrf_reflection.cfg", mock_cfg)

    def test_empty_db_returns_insufficient_data(self):
        """空 DB（无决策事件）→ status: insufficient_data。"""
        from cvrf_reflection import run_weekly_gc
        tmp_db, tmp_out = self._setup_isolated_gc()
        with patch("cvrf_reflection.DB_PATH", tmp_db), \
             self._patch_cfg_path(tmp_out):
            result = run_weekly_gc(weeks_back=4)
            self.assertEqual(result["status"], "insufficient_data")
            self.assertEqual(result["total_events"], 0)
            self.assertIn("message", result)
            self.assertTrue(os.path.exists(tmp_out))
        os.unlink(tmp_db)
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)

    def test_high_win_rate_triggers_tighten(self):
        """胜率 > 60% → action: tighten + 调 tune_thresholds/add_pattern。"""
        from cvrf_reflection import run_weekly_gc
        tmp_db, tmp_out = self._setup_isolated_gc()
        events = [
            {"date": "2026-06-25", "code": "002049", "action": "BUY",
             "rationale": "急跌-3%+cp<0.4+close>MA20"},
            {"date": "2026-06-26", "code": "002049", "action": "BUY",
             "rationale": "急跌-2%+cp<0.3+close>MA20"},
            {"date": "2026-06-27", "code": "000938", "action": "BUY",
             "rationale": "急跌-3%+cp<0.4+close>MA20"},
            {"date": "2026-06-28", "code": "002049", "action": "BUY",
             "rationale": "急跌-2.5%+cp<0.35+close>MA20"},
            {"date": "2026-06-28", "code": "000938", "action": "BUY",
             "rationale": "急跌-3%+cp<0.4+close>MA20"},
            {"date": "2026-06-29", "code": "002049", "action": "BUY",
             "rationale": "急跌-2%+cp<0.3+close>MA20"},
        ]
        self._insert_decision_events(tmp_db, events)

        import sys
        mock_module = MagicMock()
        mock_module.tune_thresholds = MagicMock(return_value={"changes": {"rapid_drop": "-3.0 → -2.7"}})
        mock_module.add_pattern = MagicMock()

        with patch("cvrf_reflection.DB_PATH", tmp_db), \
             self._patch_cfg_path(tmp_out), \
             patch("cvrf_reflection._evaluate_event_outcome") as mock_eval, \
             patch.dict(sys.modules, {"stock_signal_profile": mock_module}):
            mock_eval.side_effect = ["win", "win", "win", "win", "win", "loss"]
            result = run_weekly_gc(weeks_back=4)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["clusters"]), 1)
        cluster = result["clusters"][0]
        self.assertEqual(cluster["action"], "tighten")
        self.assertGreaterEqual(cluster["win_rate"], 0.6)
        self.assertGreater(result["profile_mutations"], 0)
        os.unlink(tmp_db)
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)

    def test_low_win_rate_triggers_remove(self):
        """胜率 < 40% → action: remove + 调 add_pattern 标记移除。"""
        from cvrf_reflection import run_weekly_gc
        tmp_db, tmp_out = self._setup_isolated_gc()
        events = [
            {"date": "2026-06-25", "code": "000063", "action": "SELL",
             "rationale": "rolling_decline+止损未触发"},
            {"date": "2026-06-26", "code": "000063", "action": "SELL",
             "rationale": "rolling_decline+止损未触发"},
            {"date": "2026-06-27", "code": "000063", "action": "SELL",
             "rationale": "rolling_decline+止损未触发"},
            {"date": "2026-06-28", "code": "000063", "action": "SELL",
             "rationale": "rolling_decline+止损未触发"},
            {"date": "2026-06-29", "code": "000063", "action": "SELL",
             "rationale": "rolling_decline+止损未触发"},
        ]
        self._insert_decision_events(tmp_db, events)

        import sys
        mock_module = MagicMock()
        mock_module.tune_thresholds = MagicMock(return_value={"changes": {}})
        mock_module.add_pattern = MagicMock()

        with patch("cvrf_reflection.DB_PATH", tmp_db), \
             self._patch_cfg_path(tmp_out), \
             patch("cvrf_reflection._evaluate_event_outcome") as mock_eval, \
             patch.dict(sys.modules, {"stock_signal_profile": mock_module}):
            mock_eval.side_effect = ["loss", "loss", "loss", "loss", "win"]
            result = run_weekly_gc(weeks_back=4)
        self.assertEqual(result["status"], "ok")
        cluster = result["clusters"][0]
        self.assertEqual(cluster["action"], "remove")
        self.assertLess(cluster["win_rate"], 0.4)
        os.unlink(tmp_db)
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)

    def test_mid_win_rate_maintains(self):
        """胜率 40-60% → action: maintain（不动 profile）。"""
        from cvrf_reflection import run_weekly_gc
        tmp_db, tmp_out = self._setup_isolated_gc()
        events = [
            {"date": "2026-06-25", "code": "002049", "action": "BUY",
             "rationale": "急跌-3%+cp<0.4"},
            {"date": "2026-06-26", "code": "002049", "action": "BUY",
             "rationale": "急跌-2%+cp<0.3"},
            {"date": "2026-06-27", "code": "002049", "action": "BUY",
             "rationale": "急跌-3%+cp<0.4"},
            {"date": "2026-06-28", "code": "002049", "action": "BUY",
             "rationale": "急跌-2.5%+cp<0.35"},
            {"date": "2026-06-29", "code": "002049", "action": "BUY",
             "rationale": "急跌-3%+cp<0.4"},
        ]
        self._insert_decision_events(tmp_db, events)

        with patch("cvrf_reflection.DB_PATH", tmp_db), \
             self._patch_cfg_path(tmp_out), \
             patch("cvrf_reflection._evaluate_event_outcome") as mock_eval:
            mock_eval.side_effect = ["win", "loss", "win", "loss", "loss"]
            result = run_weekly_gc(weeks_back=4)
        self.assertEqual(result["status"], "ok")
        cluster = result["clusters"][0]
        self.assertEqual(cluster["action"], "maintain")
        self.assertEqual(result["profile_mutations"], 0)
        os.unlink(tmp_db)
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)

    def test_small_cluster_skipped(self):
        """聚类 < MIN_CLUSTER_SIZE(5) → action: skip。"""
        from cvrf_reflection import run_weekly_gc, MIN_CLUSTER_SIZE
        tmp_db, tmp_out = self._setup_isolated_gc()
        events = [
            {"date": "2026-06-25", "code": "002049", "action": "BUY",
             "rationale": "急跌-3%+cp<0.4"},
            {"date": "2026-06-26", "code": "002049", "action": "BUY",
             "rationale": "急跌-2%+cp<0.3"},
            {"date": "2026-06-27", "code": "002049", "action": "BUY",
             "rationale": "急跌-3%+cp<0.4"},
        ]
        self._insert_decision_events(tmp_db, events)
        with patch("cvrf_reflection.DB_PATH", tmp_db), \
             self._patch_cfg_path(tmp_out):
            result = run_weekly_gc(weeks_back=4)
        self.assertEqual(result["status"], "ok")
        cluster = result["clusters"][0]
        self.assertEqual(cluster["action"], "skip")
        self.assertEqual(result["profile_mutations"], 0)
        os.unlink(tmp_db)
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)


class TestSaveGcReport(unittest.TestCase):
    """测试 _save_gc_report 原子写入。"""

    def test_writes_json_file(self):
        import cvrf_reflection
        from cvrf_reflection import _save_gc_report
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        report = {"status": "ok", "test": True}
        mock_cfg = MagicMock()
        mock_cfg.path.cvrf_weekly_gc = tmp_out
        mock_cfg.root = cvrf_reflection.cfg.root
        with patch("cvrf_reflection.cfg", mock_cfg):
            _save_gc_report(report)
            self.assertTrue(os.path.exists(tmp_out))
            with open(tmp_out) as f:
                loaded = json.load(f)
            self.assertEqual(loaded["status"], "ok")
            self.assertTrue(loaded["test"])
        os.unlink(tmp_out)


class TestCli(unittest.TestCase):
    """测试 CLI --mode 入口。"""

    def test_weekly_gc_mode_runs(self):
        """`--mode weekly-gc` 应调 run_weekly_gc 并打印 JSON。"""
        import subprocess
        result = subprocess.run(
            ["/root/ai_trading_package/quant_env/bin/python3",
             "/root/ai_trading_package/quant/quant_scripts/cvrf_reflection.py",
             "--mode", "weekly-gc", "--weeks-back", "4"],
            capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertIn("status", output)
        self.assertIn("weeks_back", output)
        self.assertEqual(output["weeks_back"], 4)

    def test_nightly_mode_runs(self):
        """`--mode nightly`（默认）应调 main()，不抛 NameError。"""
        import subprocess
        result = subprocess.run(
            ["/root/ai_trading_package/quant_env/bin/python3",
             "/root/ai_trading_package/quant/quant_scripts/cvrf_reflection.py",
             "--mode", "nightly"],
            capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("NameError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
