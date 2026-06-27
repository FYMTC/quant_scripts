import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator
from unittest.mock import patch


ROOT = Path("/root/ai_trading_package/quant/quant_scripts")
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
                "QUANT_TRADE_DB_PATH": str(self.trade_log_path),
            }
        )
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def snapshot(self) -> Dict:
        path = self.scenario_dir / "account_snapshot.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def seed_baseline_files(self) -> None:
        snapshot = self.snapshot()
        cash = float(snapshot.get("cash") or 0.0)
        total_value = float(snapshot.get("total_value") or 0.0)
        positions = snapshot.get("positions") or []
        holdings = [
            {
                "code": row.get("code"),
                "name": row.get("name") or row.get("code"),
                "shares": row.get("shares") or 0,
                "cost": row.get("cost") or 0,
                "price": row.get("last_price") or row.get("cost") or 0,
                "market_value": row.get("market_value") or 0,
                "pnl": row.get("profit") or 0,
                "pnl_pct": 0.0,
                "change_pct": 0.0,
                "n_days": 0,
            }
            for row in positions
        ]
        self.write_json(
            "feature_snapshot.json",
            {
                "generated_at": "2026-05-23T08:45:00",
                "as_of_date": "2026-05-23",
                "source_modules": ["tests.runtime_sandbox"],
                "runtime_flags": {"feature_fresh": True, "missing_codes": []},
                "portfolio": {
                    "market_regime": {"ok": True, "current_state": "neutral"},
                    "event_risk": {"source": "scenario", "score": 0.2},
                },
                "per_stock": {
                    "300408": {"risk_level": "medium", "cvar": 4.2},
                    "000063": {"risk_level": "medium", "cvar": 3.8},
                },
            },
        )
        self.write_json(
            "screener_top15.json",
            {
                "results": [
                    {"code": "300408", "name": "三环集团", "composite_score": 1.8},
                    {"code": "000063", "name": "中兴通讯", "composite_score": 1.6},
                    {"code": "002475", "name": "立讯精密", "composite_score": 1.4},
                ]
            },
        )
        self.write_json(
            "close_output.json",
            {
                "generated_at": "2026-05-23T15:05:00",
                "holdings": holdings,
                "cash": cash,
                "total_assets": total_value,
                "recommendation": "READY",
                "constraints": [],
                "alerts": [],
            },
        )
        self.write_json("night_quant.json", {"ok": True, "source": "scenario"})

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
            "night_quant.json",
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
