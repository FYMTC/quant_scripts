#!python3

import argparse
import subprocess
import sys
from system_config import cfg

VENV_PY = cfg.python
# fast suite：轻量单元测试（outbox/account_context/desk），~5s
FAST_MODULES = [
    "tests.test_trade_outbox",
    "tests.test_trade_account_context",
    "tests.test_agent_desk",
]
# cycle suite：重量级端到端集成测试（跑完整 morning/review app 子链，~245s）
INTEGRATION_MODULES = [
    "tests.test_runtime_integration",
]
CYCLE_MODULES = [
    "tests.test_trade_day_cycle",
]


def run_modules(modules: list[str]) -> int:
    cmd = [VENV_PY, "-m", "unittest", *modules]
    return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["fast", "cycle"], default="fast")
    args = ap.parse_args()
    if args.suite == "fast":
        return run_modules(FAST_MODULES)
    # cycle suite: 完整集成 + cycle
    for modules in (INTEGRATION_MODULES, CYCLE_MODULES):
        rc = run_modules(modules)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
