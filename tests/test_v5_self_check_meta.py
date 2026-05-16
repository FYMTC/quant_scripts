#!/config/quant_env/bin/python3
"""v5_self_check 元测试：模块列表完整。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import v5_self_check as sc  # noqa: E402

EXPECTED_TEST_MODULES = (
    "tests.test_constraints",
    "tests.test_agent_queue",
    "tests.test_trade_outbox",
    "tests.test_v5_contracts",
    "tests.test_decision_gate",
    "tests.test_signal_loop",
    "tests.test_agent_desk",
    "tests.test_digest",
)


class TestSelfCheckRegistry(unittest.TestCase):
    def test_all_modules_importable(self):
        import unittest as ut

        loader = ut.TestLoader()
        for mod in EXPECTED_TEST_MODULES:
            suite = loader.loadTestsFromName(mod)
            self.assertGreater(suite.countTestCases(), 0, msg=mod)


if __name__ == "__main__":
    unittest.main()
