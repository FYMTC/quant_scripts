#!python3

import argparse
import subprocess
import sys
from system_config import cfg

VENV_PY = cfg.python
FAST_MODULES = [
    "tests.test_trade_outbox",
    "tests.test_trade_account_context",
    "tests.test_agent_desk",
]
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
        for modules in (FAST_MODULES, INTEGRATION_MODULES):
            rc = run_modules(modules)
            if rc != 0:
                return rc
        return 0
    for modules in (INTEGRATION_MODULES, CYCLE_MODULES):
        rc = run_modules(modules)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
