#!/config/quant_env/bin/python3

import json
import subprocess
import unittest

from tests.runtime_sandbox import sandboxed_runtime

VENV_PY = "/config/quant_env/bin/python3"
MORNING_PLAN_APP = "/config/.hermes/scripts/morning_plan_app.py"
AGENT_DESK = "/config/quant_scripts/agent_desk.py"
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

            desk_proc = subprocess.run(
                [VENV_PY, AGENT_DESK, "--json", "--max", "5"],
                capture_output=True,
                text=True,
                timeout=300,
                env=sandbox.env(),
            )
            self.assertEqual(desk_proc.returncode, 0, desk_proc.stderr or desk_proc.stdout)
            desk_out = json.loads((desk_proc.stdout or "{}").strip())
            self.assertTrue(desk_out.get("needs_hermes"))

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
            self.assertGreaterEqual(len(morning.get("buy_proposals") or []), 2)
            self.assertGreaterEqual(pending.get("count") or 0, 1)
            self.assertTrue(plan.get("wechat_work_report_body"))
            self.assertTrue(review.get("wechat_work_report_body"))
            self.assertIn(night.get("recommendation"), ("READY", "CAUTION", "BLOCKED"))

            outbox_path = sandbox.root / "trade_wechat_outbox.jsonl"
            self.assertTrue(outbox_path.is_file())
            lines = [line for line in outbox_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertGreaterEqual(len(lines), 1)
            self.assertTrue(any("【买卖请示】" in json.loads(line).get("body", "") for line in lines))


if __name__ == "__main__":
    unittest.main()
