"""T1.10 二期 trade_db 扩展单元测试（2026-06-30）

测试 TradeDB 的 trading_journal 表扩展：
  - ALTER TABLE 幂等迁移
  - log_trade_event() 写入新字段
  - query() 返回扩展列 + 按 action 过滤
  - 旧 log() 向后兼容

测试用临时 db path，不污染真实 trade_log.db。
"""

import unittest
import sys
import os
import json
import tempfile

sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")
os.chdir("/root/ai_trading_package/quant/quant_scripts")


def _make_isolated_db():
    """创建一个用临时文件的 TradeDB 实例，返回 (db, tmp_path)。"""
    import trade_db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    # 直接实例化 TradeDB 并覆盖 db_path
    db = trade_db.TradeDB.__new__(trade_db.TradeDB)
    db.db_path = tmp.name
    db._ensure_db()
    return db, tmp.name


class TestAlterTableMigration(unittest.TestCase):
    """测试 ALTER TABLE 幂等迁移"""

    def test_multiple_init_no_error(self):
        """多次实例化 TradeDB 不报错（列已存在时吞掉 OperationalError）"""
        db1, path = _make_isolated_db()
        # 第二次实例化（同一 db_path）应不抛异常
        import trade_db
        db2 = trade_db.TradeDB.__new__(trade_db.TradeDB)
        db2.db_path = path
        db2._ensure_db()  # 不抛
        # 第三次
        db3 = trade_db.TradeDB.__new__(trade_db.TradeDB)
        db3.db_path = path
        db3._ensure_db()  # 不抛
        os.unlink(path)

    def test_new_columns_exist(self):
        """扩展列存在"""
        db, path = _make_isolated_db()
        import sqlite3
        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(trading_journal)").fetchall()]
        conn.close()
        for expected in ("action", "event_id", "signal_id", "request_id",
                         "resolver_path", "decision_gate_json", "rationale"):
            self.assertIn(expected, cols, f"missing column: {expected}")
        os.unlink(path)


class TestLogTradeEvent(unittest.TestCase):
    """测试 log_trade_event() 方法"""

    def test_basic_write_and_query(self):
        """写入后 query 能查到 action/resolver_path"""
        db, path = _make_isolated_db()
        db.log_trade_event(
            code="002049", name="紫光国微", action="BUY",
            signal_id="002049_rapid_drop",
            resolver_path="sig=rapid_drop holding=False → bf=0.72 → BUY",
            decision_gate={"verdict": "APPROVE"},
            rationale="抄底",
        )
        rows = db.query(type="决策事件", limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "BUY")
        self.assertEqual(rows[0]["code"], "002049")
        self.assertIn("→", rows[0]["resolver_path"])
        os.unlink(path)

    def test_full_fields_persisted(self):
        """所有字段正确落库"""
        db, path = _make_isolated_db()
        db.log_trade_event(
            code="000063", name="中兴通讯", action="SELL",
            event_id="evt_001", signal_id="sig_001",
            request_id="req_001",
            resolver_path="sig=rapid_drop holding=True stop=True → SELL",
            decision_gate={"verdict": "APPROVE", "direction": "SELL", "gates": []},
            rationale="止损触发",
        )
        rows = db.query(type="决策事件", limit=5)
        r = rows[0]
        self.assertEqual(r["action"], "SELL")
        self.assertEqual(r["event_id"], "evt_001")
        self.assertEqual(r["signal_id"], "sig_001")
        self.assertEqual(r["request_id"], "req_001")
        self.assertIn("stop=True", r["resolver_path"])
        self.assertEqual(r["decision_gate"]["direction"], "SELL")
        self.assertEqual(r["rationale"], "止损触发")
        os.unlink(path)

    def test_legacy_log_still_works(self):
        """旧 log() 方法不破（向後兼容）"""
        db, path = _make_isolated_db()
        db.log("异动", "000063", "中兴通讯", "大跌-4.16%", {"pct": -4.16})
        rows = db.query(type="异动", limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "000063")
        self.assertEqual(rows[0]["detail"]["pct"], -4.16)
        # 旧记录的扩展列应为空字符串
        self.assertEqual(rows[0]["action"], "")
        self.assertEqual(rows[0]["resolver_path"], "")
        os.unlink(path)

    def test_query_by_action(self):
        """按 action 过滤"""
        db, path = _make_isolated_db()
        db.log_trade_event(code="002049", action="BUY", resolver_path="→ BUY")
        db.log_trade_event(code="000063", action="SELL", resolver_path="→ SELL")
        db.log_trade_event(code="600487", action="HOLD", resolver_path="→ HOLD")

        buys = db.query(action="BUY", limit=10)
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["code"], "002049")

        sells = db.query(action="SELL", limit=10)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["code"], "000063")
        os.unlink(path)

    def test_decision_gate_json_roundtrip(self):
        """decision_gate JSON 序列化/反序列化"""
        db, path = _make_isolated_db()
        gate = {"verdict": "APPROVE", "direction": "BUY", "gates": [{"pass": True, "message": "ok"}]}
        db.log_trade_event(
            code="002049", action="BUY",
            resolver_path="→ BUY", decision_gate=gate,
        )
        rows = db.query(type="决策事件", limit=5)
        self.assertEqual(rows[0]["decision_gate"]["verdict"], "APPROVE")
        self.assertEqual(rows[0]["decision_gate"]["gates"][0]["pass"], True)
        os.unlink(path)

    def test_empty_resolver_path(self):
        """空 resolver_path 不报错"""
        db, path = _make_isolated_db()
        db.log_trade_event(code="002049", action="HOLD", resolver_path="")
        rows = db.query(type="决策事件", limit=5)
        self.assertEqual(rows[0]["resolver_path"], "")
        self.assertEqual(rows[0]["action"], "HOLD")
        os.unlink(path)

    def test_rationale_truncation(self):
        """超长 rationale 截断到 500"""
        db, path = _make_isolated_db()
        long_rationale = "x" * 1000
        db.log_trade_event(
            code="002049", action="BUY",
            resolver_path="→ BUY", rationale=long_rationale,
        )
        rows = db.query(type="决策事件", limit=5)
        self.assertEqual(len(rows[0]["rationale"]), 500)
        os.unlink(path)

    def test_message_field_format(self):
        """message 字段格式 = '{action} {code} ({resolver_path[:80]})'"""
        db, path = _make_isolated_db()
        db.log_trade_event(
            code="002049", action="BUY",
            resolver_path="sig=rapid_drop → BUY",
        )
        rows = db.query(type="决策事件", limit=5)
        self.assertIn("BUY", rows[0]["message"])
        self.assertIn("002049", rows[0]["message"])
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()
