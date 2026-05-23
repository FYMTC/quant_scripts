import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator
from unittest.mock import patch


ROOT = Path("/config/quant_scripts")
DATA = ROOT / "data"
TESTS = ROOT / "tests"
SCENARIOS = TESTS / "scenarios"


class RuntimeSandbox:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.root = Path(tempfile.mkdtemp(prefix=f"quant-sim-{scenario}-"))
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.root / "signal_audit.jsonl"
        self.trade_log_path = self.root / "trade_log.db"
        self.scenario_dir = SCENARIOS / scenario
        self._ensure_parent_files()

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write_json(self, name: str, payload: Dict) -> Path:
        path = self.data_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_json(self, name: str) -> Dict:
        path = self.data_dir / name
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def write_jsonl(self, name: str, rows) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def env(self, **extra: str) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "QUANT_RUNTIME_ROOT": str(self.root),
                "QUANT_RUNTIME_DATA_DIR": str(self.data_dir),
                "QUANT_RUNTIME_SCENARIO": self.scenario,
                "QUANT_NOTIFY_MODE": "record-only",
            }
        )
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def snapshot(self) -> Dict:
        path = self.scenario_dir / "account_snapshot.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _ensure_parent_files(self) -> None:
        for name in (
            "agent_state.json",
            "trade_request_pending.json",
            "morning_output.json",
            "plan_bundle.json",
            "close_output.json",
            "night_output.json",
            "review_bundle.json",
            "feature_snapshot.json",
            "screener_top15.json",
        ):
            path = self.data_dir / name
            if not path.exists():
                path.write_text("{}\n", encoding="utf-8")
        if not self.audit_path.exists():
            self.audit_path.write_text("", encoding="utf-8")
        if not self.trade_log_path.exists():
            self.trade_log_path.write_text("", encoding="utf-8")


@contextmanager
def sandboxed_runtime(scenario: str) -> Iterator[RuntimeSandbox]:
    sandbox = RuntimeSandbox(scenario)
    try:
        yield sandbox
    finally:
        sandbox.cleanup()


@contextmanager
def fake_account_snapshot(snapshot: Dict):
    import trade_account_context as tac

    with patch.object(tac, "load_account_snapshot", return_value=snapshot), patch.object(
        tac, "load_portfolio_truth", return_value=tac.normalize_portfolio_truth(snapshot)
    ):
        yield
