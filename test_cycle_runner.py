#!/config/quant_env/bin/python3

import argparse
import subprocess
import sys

VENV_PY = "/config/quant_env/bin/python3"
UNIT_MODULES = [
    "tests.test_trade_outbox",
    "tests.test_trade_account_context",
    "tests.test_agent_desk",
    "tests.test_runtime_integration",
]
CYCLE_MODULES = [
    "tests.test_runtime_integration",
    "tests.test_trade_day_cycle",
]


def run_modules(modules: list[str]) -> int:
    cmd = [VENV_PY, "-m", "unittest", *modules]
    return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["fast", "cycle"], default="fast")
    args = ap.parse_args()
    modules = UNIT_MODULES if args.suite == "fast" else CYCLE_MODULES
    return run_modules(modules)


if __name__ == "__main__":
    sys.exit(main())
