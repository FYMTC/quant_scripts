"""T1.10 二期 close_loop_reflow 单元测试（2026-06-30）

测试清仓标的回流窗口管理：
  - record_clear: 记录清仓 + expire_at 计算 + reflow_days clamp + 同 code 覆盖
  - is_in_reflow: 窗口内/过期/无记录
  - get_reflow_codes: 多代码/过滤过期
  - get_reflow_record: 查单条
  - prune_expired: 删过期/保留有效/返回计数
  - cli: list/prune 子命令

隔离：每个测试用 tempfile + patch STATE_PATH，不污染真实 close_loop_reflow.json。
"""

import unittest
import sys
import os
import tempfile
import json
from unittest.mock import patch
from datetime import datetime, timedelta

sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")
os.chdir("/root/ai_trading_package/quant/quant_scripts")

import close_loop_reflow as clr
from close_loop_reflow import (
    record_clear,
    is_in_reflow,
    get_reflow_codes,
    get_reflow_record,
    prune_expired,
    REFLOW_DAYS_MIN,
    REFLOW_DAYS_MAX,
    REFLOW_DAYS_DEFAULT,
)


class _IsolatedStateTestCase(unittest.TestCase):
    """测试基类：每个测试用独立 tempfile，setUp/tearDown 清空。"""

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".json", prefix="clr_test_")
        os.close(fd)
        # 清空初始内容
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump({"records": [], "updated_at": ""}, f)
        # patch 模块级 STATE_PATH（_load_state/_save_state 直接读模块全局）
        self._patch = patch("close_loop_reflow.STATE_PATH", self.tmp_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass


class TestRecordClear(_IsolatedStateTestCase):
    """测试 record_clear"""

    def test_basic_write(self):
        """基本写入：返回 record，含必填字段"""
        r = record_clear("002475", "立讯", sell_price=69.99, shares=600,
                         account_id="manual_main", signal_id="rolling_decline")
        self.assertEqual(r["code"], "002475")
        self.assertEqual(r["name"], "立讯")
        self.assertEqual(r["sell_price"], 69.99)
        self.assertEqual(r["shares"], 600)
        self.assertEqual(r["account_id"], "manual_main")
        self.assertEqual(r["signal_id"], "rolling_decline")
        self.assertIn("sold_at", r)
        self.assertIn("sold_date", r)
        self.assertIn("expire_at", r)

    def test_default_reflow_days(self):
        """缺省 reflow_days = REFLOW_DAYS_DEFAULT"""
        r = record_clear("000063")
        self.assertEqual(r["reflow_days"], REFLOW_DAYS_DEFAULT)

    def test_custom_reflow_days_clamp_min(self):
        """reflow_days < MIN → clamp 到 MIN"""
        r = record_clear("000063", reflow_days=1)
        self.assertEqual(r["reflow_days"], REFLOW_DAYS_MIN)

    def test_custom_reflow_days_clamp_max(self):
        """reflow_days > MAX → clamp 到 MAX"""
        r = record_clear("000063", reflow_days=99)
        self.assertEqual(r["reflow_days"], REFLOW_DAYS_MAX)

    def test_expire_at_offset(self):
        """expire_at ≈ now + reflow_days"""
        before = datetime.now()
        r = record_clear("000063", reflow_days=7)
        after = datetime.now()
        expire = datetime.fromisoformat(r["expire_at"])
        # expire 应在 [before+7d, after+7d] 区间
        self.assertGreaterEqual(expire, before + timedelta(days=7) - timedelta(seconds=1))
        self.assertLessEqual(expire, after + timedelta(days=7) + timedelta(seconds=1))

    def test_same_code_overwrites(self):
        """同 code 二次记录覆盖旧记录（保留最新清仓）"""
        r1 = record_clear("002475", "立讯", sell_price=69.99, shares=600)
        r2 = record_clear("002475", "立讯", sell_price=70.50, shares=600)
        self.assertEqual(r2["sell_price"], 70.50)
        # 直接读文件验证：应只有一条 002475 且 sell_price 为最新值
        with open(self.tmp_path, encoding="utf-8") as f:
            data = json.load(f)
        same_codes = [r for r in data["records"] if r["code"] == "002475"]
        self.assertEqual(len(same_codes), 1)
        self.assertEqual(same_codes[0]["sell_price"], 70.50)

    def test_empty_code_returns_empty(self):
        """空 code → 返回 {}，不写入"""
        r = record_clear("")
        self.assertEqual(r, {})


class TestIsInReflow(_IsolatedStateTestCase):
    """测试 is_in_reflow"""

    def test_in_window_true(self):
        """窗口内 → True"""
        record_clear("002475", reflow_days=7)
        self.assertTrue(is_in_reflow("002475"))

    def test_expired_false(self):
        """过期 → False（手动构造过期记录）"""
        record_clear("002475", reflow_days=7)
        # 手动把 expire_at 改成过去
        with open(self.tmp_path, encoding="utf-8") as f:
            data = json.load(f)
        data["records"][0]["expire_at"] = (datetime.now() - timedelta(days=1)).isoformat()
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        self.assertFalse(is_in_reflow("002475"))

    def test_no_record_false(self):
        """无记录 → False"""
        self.assertFalse(is_in_reflow("999999"))

    def test_empty_code_false(self):
        """空 code → False"""
        self.assertFalse(is_in_reflow(""))


class TestGetReflowCodes(_IsolatedStateTestCase):
    """测试 get_reflow_codes"""

    def test_multiple_codes(self):
        """多代码全部有效"""
        record_clear("002475", reflow_days=7)
        record_clear("000063", reflow_days=7)
        record_clear("000938", reflow_days=7)
        codes = set(get_reflow_codes())
        self.assertEqual(codes, {"002475", "000063", "000938"})

    def test_filters_expired(self):
        """过滤过期记录"""
        record_clear("002475", reflow_days=7)
        record_clear("000063", reflow_days=7)
        # 把 000063 改成过期
        with open(self.tmp_path, encoding="utf-8") as f:
            data = json.load(f)
        for r in data["records"]:
            if r["code"] == "000063":
                r["expire_at"] = (datetime.now() - timedelta(days=1)).isoformat()
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        codes = get_reflow_codes()
        self.assertEqual(codes, ["002475"])

    def test_empty_state(self):
        """空状态 → 空列表"""
        self.assertEqual(get_reflow_codes(), [])


class TestGetReflowRecord(_IsolatedStateTestCase):
    """测试 get_reflow_record"""

    def test_returns_record(self):
        """有记录 → 返回 record"""
        record_clear("002475", "立讯", sell_price=69.99, shares=600)
        r = get_reflow_record("002475")
        self.assertIsNotNone(r)
        self.assertEqual(r["code"], "002475")
        self.assertEqual(r["name"], "立讯")

    def test_no_record_returns_none(self):
        """无记录 → None"""
        self.assertIsNone(get_reflow_record("999999"))

    def test_returns_record_even_if_expired(self):
        """过期记录仍返回（不检查过期，仅 is_in_reflow 检查）"""
        record_clear("002475", reflow_days=7)
        with open(self.tmp_path, encoding="utf-8") as f:
            data = json.load(f)
        data["records"][0]["expire_at"] = (datetime.now() - timedelta(days=1)).isoformat()
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        r = get_reflow_record("002475")
        self.assertIsNotNone(r, "get_reflow_record 应返回过期记录（不检查过期）")


class TestPruneExpired(_IsolatedStateTestCase):
    """测试 prune_expired"""

    def test_removes_expired(self):
        """删除过期记录"""
        record_clear("002475", reflow_days=7)
        record_clear("000063", reflow_days=7)
        # 把 000063 改成过期
        with open(self.tmp_path, encoding="utf-8") as f:
            data = json.load(f)
        for r in data["records"]:
            if r["code"] == "000063":
                r["expire_at"] = (datetime.now() - timedelta(days=1)).isoformat()
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        n = prune_expired()
        self.assertEqual(n, 1)
        # 002475 仍在
        self.assertTrue(is_in_reflow("002475"))
        self.assertFalse(is_in_reflow("000063"))

    def test_keeps_all_valid(self):
        """全部有效 → 删除 0 条"""
        record_clear("002475", reflow_days=7)
        record_clear("000063", reflow_days=7)
        n = prune_expired()
        self.assertEqual(n, 0)
        self.assertEqual(len(get_reflow_codes()), 2)

    def test_empty_state_returns_zero(self):
        """空状态 → 0"""
        self.assertEqual(prune_expired(), 0)


class TestCli(_IsolatedStateTestCase):
    """测试 CLI 入口"""

    def test_cli_list(self):
        """list 子命令输出记录"""
        record_clear("002475", "立讯", sell_price=69.99, shares=600)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["close_loop_reflow.py", "list"]
        try:
            with redirect_stdout(buf):
                clr.cli()
            output = buf.getvalue()
            self.assertIn("002475", output)
            self.assertIn("立讯", output)
            self.assertIn("回流记录共 1 条", output)
        finally:
            sys.argv = old_argv

    def test_cli_prune(self):
        """prune 子命令输出清理计数"""
        record_clear("002475", reflow_days=7)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["close_loop_reflow.py", "prune"]
        try:
            with redirect_stdout(buf):
                clr.cli()
            output = buf.getvalue()
            self.assertIn("已清理 0 条过期记录", output)
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
