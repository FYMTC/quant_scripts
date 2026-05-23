#!/config/quant_env/bin/python3

import importlib
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_desk  # noqa: E402
import trade_notify  # noqa: E402
import trade_outbox  # noqa: E402
from tests.runtime_sandbox import sandboxed_runtime

VENV_PY = "/config/quant_env/bin/python3"
MORNING_PLAN_APP = "/config/.hermes/scripts/morning_plan_app.py"
REVIEW_APP = "/config/.hermes/scripts/review_app.py"


class TestRuntimeIntegration(unittest.TestCase):
    def test_morning_plan_app_writes_sandbox_outputs(self):
        with sandboxed_runtime("baseline_ready") as sandbox:
            sandbox.seed_baseline_files()
            proc = subprocess.run(
                [VENV_PY, MORNING_PLAN_APP],
                capture_output=True,
                text=True,
                timeout=300,
                env=sandbox.env(),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            morning = sandbox.read_json("morning_output.json")
            plan = sandbox.read_json("plan_bundle.json")
            self.assertTrue(morning)
            self.assertTrue(plan)
            self.assertIn("recommendation", morning)
            self.assertEqual(plan.get("phase"), "plan")
            self.assertTrue(plan.get("wechat_work_report_body"))

    def test_review_app_writes_sandbox_outputs(self):
        with sandboxed_runtime("baseline_ready") as sandbox:
            sandbox.seed_baseline_files()
            sandbox.write_json(
                "plan_bundle.json",
                {
                    "generated_at": "2026-05-23T08:45:00",
                    "phase": "plan",
                    "feature_snapshot": {"generated_at": "2026-05-23T08:40:00"},
                    "signal_auto_generate": {"feature_snapshot_used": True, "lineage_id": "lid-plan"},
                    "explainability": {"constraints": {"summary": "ok"}},
                    "quant_bundle": {},
                    "event_risk": {},
                },
            )
            proc = subprocess.run(
                [VENV_PY, REVIEW_APP],
                capture_output=True,
                text=True,
                timeout=300,
                env=sandbox.env(),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            night = sandbox.read_json("night_output.json")
            review = sandbox.read_json("review_bundle.json")
            self.assertTrue(night)
            self.assertTrue(review)
            self.assertIn("recommendation", night)
            self.assertEqual(review.get("phase"), "review")
            self.assertTrue(review.get("wechat_work_report_body"))

    def test_agent_desk_creates_trade_request_in_sandbox(self):
        with sandboxed_runtime("baseline_ready") as sandbox:
            sandbox.seed_baseline_files()
            sandbox.write_json(
                "morning_output.json",
                {
                    "buy_proposals": [
                        {
                            "code": "300408",
                            "name": "三环集团",
                            "price": 115.0,
                            "shares": 100,
                            "rationale": "score ok",
                        }
                    ]
                },
            )
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
                    "trade_account_context.load_account_snapshot",
                    return_value=sandbox.snapshot(),
                ):
                    out = agent_desk.process_pending(max_events=5)
            finally:
                agent_desk.MORNING_OUTPUT_PATH = old_morning_path
                agent_desk.STATE_PATH = old_state_path
                trade_notify.NOTIFY_MODE = old_notify_mode
                trade_notify.OUTBOX_JSONL = old_notify_outbox
                trade_notify.DATA = old_notify_data
                trade_outbox.STATE_PATH = old_outbox_state
                trade_outbox.OUTBOX_PATH = old_outbox_pending
            self.assertTrue(out.get("needs_hermes"))
            self.assertEqual(len(out.get("planned_trade_requests") or []), 1)
            pending = sandbox.read_json("trade_request_pending.json")
            self.assertEqual(pending.get("count"), 1)
            requests = pending.get("requests") or []
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].get("direction"), "BUY")
            outbox_path = sandbox.root / "trade_wechat_outbox.jsonl"
            self.assertTrue(outbox_path.is_file())
            lines = [line for line in outbox_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertEqual(row.get("kind"), "trade_request")
            self.assertIn("【买卖请示】BUY", row.get("body") or "")


if __name__ == "__main__":
    unittest.main()
