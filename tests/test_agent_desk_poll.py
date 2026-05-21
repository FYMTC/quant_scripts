#!/config/quant_env/bin/python3
"""agent_desk_poll：静默不触发 LLM job。"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERMES_SCRIPTS = "/config/.hermes/scripts"
sys.path.insert(0, HERMES_SCRIPTS)

import agent_desk_poll_app as poll  # noqa: E402


class TestAgentDeskPoll(unittest.TestCase):
    @patch("agent_desk_poll_app.subprocess.Popen")
    @patch("agent_desk_poll_app._run_desk")
    def test_silent_no_trigger(self, run_desk, popen):
        run_desk.return_value = {"needs_hermes": False, "analyze_tasks": []}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pending.json")
            with patch.object(poll.adc, "DESK_PENDING_PATH", path):
                poll.main()
            popen.assert_not_called()
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertFalse(saved["needs_hermes"])

    @patch("agent_desk_poll_app.subprocess.Popen")
    @patch("agent_desk_poll_app._run_desk")
    def test_needs_hermes_triggers(self, run_desk, popen):
        run_desk.return_value = {"needs_hermes": True, "analyze_tasks": [{"event_id": "e1"}]}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pending.json")
            with patch.object(poll.adc, "DESK_PENDING_PATH", path):
                poll.main()
            popen.assert_called_once()
            args = popen.call_args[0][0]
            self.assertIn("a7f3e81d9llm", args)


    @patch("agent_desk_poll_app._run_desk")
    def test_stdout_json_parses_with_noise(self, run_desk):
        run_desk.return_value = {"needs_hermes": False, "analyze_tasks": []}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "pending.json")
            with patch.object(poll.adc, "DESK_PENDING_PATH", path):
                poll.main()
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertFalse(saved["needs_hermes"])


if __name__ == "__main__":
    unittest.main()
