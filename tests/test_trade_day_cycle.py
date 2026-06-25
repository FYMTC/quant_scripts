#!python3

import json
import os
import sys
import subprocess
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_desk  # noqa: E402
import trade_notify  # noqa: E402
import trade_outbox  # noqa: E402
from system_config import cfg  # noqa: E402
from tests.runtime_sandbox import sandboxed_runtime

VENV_PY = cfg.python
MORNING_PLAN_APP = "/config/.hermes/scripts/morning_plan_app.py"
REVIEW_APP = "/config/.hermes/scripts/review_app.py"


class TestTradeDayCycle(unittest.TestCase):
    def test_baseline_ready_cycle(self):
        with sandboxed_runtime("baseline_ready") as sandbox:
            sandbox.seed_baseline_files()

            morning_proc = subprocess.run(
                [VENV_PY, MORNING_PLAN_APP],
                capture_output=True,
                text=True,
                timeout=300,
                env=sandbox.env(),
            )
            self.assertEqual(morning_proc.returncode, 0, morning_proc.stderr or morning_proc.stdout)

            old_morning_path = agent_desk.MORNING_OUTPUT_PATH
            old_state_path = agent_desk.STATE_PATH
            old_notify_mode = trade_notify.NOTIFY_MODE
            old_notify_outbox = trade_notify.OUTBOX_JSONL
            old_notify_data = trade_notify.DATA
            old_outbox_state = trade_outbox.STATE_PATH
            old_outbox_pending = trade_outbox.OUTBOX_PATH
            agent_desk.MORNING_OUTPUT_PATH = str(sandbox.data_dir / "morning_output.json")
            agent_desk.STATE_PATH = str(sandbox.data_dir / "agent_state.json")
            trade_notify.NOTIFY_MODE = "record-only"
            trade_notify.OUTBOX_JSONL = sandbox.root / "trade_wechat_outbox.jsonl"
            trade_notify.DATA = sandbox.data_dir
            trade_outbox.STATE_PATH = str(sandbox.data_dir / "agent_state.json")
            trade_outbox.OUTBOX_PATH = str(sandbox.data_dir / "trade_request_pending.json")
            try:
                with patch("trade_accounts.resolve_trading_account", return_value="paper_easyths"), patch(
                    "trade_accounts.auto_execute_on_resolve", return_value=True
                ), patch(
                    "trade_outbox.propose_and_notify",
                    return_value={
                        "ok": True,
                        "request_id": "cycle-req-1",
                        "account_id": "paper_easyths",
                        "auto_execute": True,
                        "resolved_status": "resolved",
                        "execution": {"ok": True},
                    },
                ), patch(
                    "trade_account_context.load_account_snapshot",
                    return_value=sandbox.snapshot(),
                ):
                    desk_out = agent_desk.process_pending(max_events=5)
            finally:
                agent_desk.MORNING_OUTPUT_PATH = old_morning_path
                agent_desk.STATE_PATH = old_state_path
                trade_notify.NOTIFY_MODE = old_notify_mode
                trade_notify.OUTBOX_JSONL = old_notify_outbox
                trade_notify.DATA = old_notify_data
                trade_outbox.STATE_PATH = old_outbox_state
                trade_outbox.OUTBOX_PATH = old_outbox_pending
            self.assertIsInstance(desk_out, dict)

            review_proc = subprocess.run(
                [VENV_PY, REVIEW_APP],
                capture_output=True,
                text=True,
                timeout=300,
                env=sandbox.env(),
            )
            self.assertEqual(review_proc.returncode, 0, review_proc.stderr or review_proc.stdout)

            morning = sandbox.read_json("morning_output.json")
            plan = sandbox.read_json("plan_bundle.json")
            pending = sandbox.read_json("trade_request_pending.json")
            night = sandbox.read_json("night_output.json")
            review = sandbox.read_json("review_bundle.json")

            self.assertTrue(morning)
            self.assertTrue(plan)
            self.assertTrue(night)
            self.assertTrue(review)
            self.assertIsInstance(morning.get("buy_proposals") or [], list)
            self.assertEqual(pending.get("count") or 0, 0)
            self.assertTrue(plan.get("wechat_work_report_body"))
            self.assertTrue(review.get("wechat_work_report_body"))
            self.assertTrue((plan.get("wechat_enqueue") or {}).get("ok"))
            self.assertTrue((review.get("wechat_enqueue") or {}).get("ok"))
            self.assertEqual((plan.get("wechat_enqueue") or {}).get("kind"), "work_report")
            self.assertEqual((review.get("wechat_enqueue") or {}).get("kind"), "work_report")
            self.assertIn(night.get("recommendation"), ("READY", "CAUTION", "BLOCKED"))

            outbox_path = sandbox.root / "trade_wechat_outbox.jsonl"
            self.assertTrue(outbox_path.is_file())
            rows = [json.loads(line) for line in outbox_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            report_rows = [row for row in rows if row.get("kind") == "work_report"]
            self.assertEqual(len(report_rows), 2)
            report_types = {((row.get("meta") or {}).get("report_type")) for row in report_rows}
            self.assertEqual(report_types, {"②工作报告-早计划", "②工作报告-晚复盘"})
            phases = {((row.get("meta") or {}).get("phase")) for row in report_rows}
            self.assertEqual(phases, {"plan", "review"})


if __name__ == "__main__":
    unittest.main()
