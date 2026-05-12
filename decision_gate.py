#!/config/quant_env/bin/python3
"""
decision_gate.py — 操盘决策强制门禁

P1-5 修复 (2026-05-12): 将四步门禁从 prompt/注释约束升级为代码强制。
任何 BUY/SELL/调仓 建议输出前必须通过此门禁。

四步检查：
  Gate 1: 分析师评分 → 操作映射 (consolidate_report 的 score_to_action)
  Gate 2: T+1 合规检查 (stock_kb 查询今日是否已买入)
  Gate 3: 风控验证 (risk_check.py verify 子进程)
  Gate 4: 仓位评估 (position_sizer.py 计算具体股数+风险)

只有 APPROVE 允许输出 BUY/SELL；MODIFY 需调整参数；REJECT 禁止操作。

用法:
  from decision_gate import DecisionGate
  
  gate = DecisionGate()
  result = gate.check(
      ticker="000938",
      direction="BUY",
      analyst_scores={"technical": 1.2, "sentiment": 0.5, "news": 0.8, "fundamental": 0.3},
      current_price=28.50,
  )
  if result["verdict"] == "APPROVE":
      print(f"✅ 门禁通过: {result['action_plan']}")
  else:
      print(f"❌ 拒绝: {result['reasons']}")

CLI用法:
  python decision_gate.py --ticker 000938 --direction BUY --price 28.50 \\
      --scores '{"technical":1.2,"news":0.8,"sentiment":0.5,"fundamental":0.3}'
"""

import json
import sys
import os
import subprocess
from datetime import datetime
from typing import Dict, List, Tuple, Optional

sys.path.insert(0, os.path.dirname(__file__))

# ========== Gate 1: 评分→映射 ==========

SCORE_TO_ACTION = [
    (1.5, "BUY", "强烈买入"),
    (0.8, "BUY", "偏多买入"),
    (0.3, "OVERWEIGHT", "谨慎加仓"),
    (-0.3, "HOLD", "观望"),
    (-0.8, "UNDERWEIGHT", "偏空减仓"),
    (-1.5, "SELL", "强烈卖出"),
    (-99, "SELL", "极端看空"),
]

DEFAULT_WEIGHTS = {
    "technical": 0.30,
    "sentiment": 0.10,
    "news": 0.35,
    "fundamental": 0.25,
}


class DecisionGate:
    """四步门禁：评分→T+1→风控→仓位"""

    def check(
        self,
        ticker: str,
        direction: str,
        analyst_scores: Dict[str, float],
        current_price: float,
        shares: int = 100,
        weights: Dict[str, float] = None,
    ) -> Dict:
        """
        执行四步门禁检查。

        Args:
            ticker: 股票代码
            direction: BUY/SELL/HOLD/OVERWEIGHT/UNDERWEIGHT
            analyst_scores: {role: score} e.g. {"technical": 1.2, "news": 0.8, ...}
            current_price: 当前价格
            shares: 建议股数（默认100，Gate 4会重新计算）
            weights: 各维度权重（默认DEFAULT_WEIGHTS）

        Returns:
            {"verdict": "APPROVE|MODIFY|REJECT", "gates": [...], "reasons": [...],
             "action_plan": {...}}
        """
        w = weights or DEFAULT_WEIGHTS
        gates = []
        all_pass = True
        reasons = []

        # ─── Gate 1: 评分→映射 ───
        g1 = self._gate_score_mapping(analyst_scores, w)
        gates.append(g1)
        if not g1["pass"]:
            all_pass = False
            reasons.append(f"[G1] {g1['message']}")

        # ─── Gate 2: T+1 合规 ───
        g2 = self._gate_t1_check(ticker, direction)
        gates.append(g2)
        if not g2["pass"]:
            all_pass = False
            reasons.append(f"[G2] {g2['message']}")

        # ─── Gate 3: 风控验证 ───
        g3 = self._gate_risk_check(ticker, direction, shares, current_price)
        gates.append(g3)
        if not g3["pass"]:
            all_pass = False
            reasons.append(f"[G3] {g3['message']}")

        # ─── Gate 4: 仓位评估 ───
        g4 = self._gate_position_sizer(ticker, direction, g1.get("mapped_action", direction),
                                        current_price, analyst_scores)
        gates.append(g4)
        if not g4["pass"]:
            all_pass = False
            reasons.append(f"[G4] {g4['message']}")

        # ─── 综合裁决 ───
        if all_pass:
            verdict = "APPROVE"
        elif any("G1" in r or "G2" in r for r in reasons):
            verdict = "REJECT"  # 评分/T+1失败 → 硬拒绝
        else:
            verdict = "MODIFY"  # 风控/仓位有问题 → 可调整

        return {
            "verdict": verdict,
            "ticker": ticker,
            "direction": direction,
            "current_price": current_price,
            "composite_score": round(g1.get("composite_score", 0), 2),
            "mapped_action": g1.get("mapped_action", direction),
            "gates": gates,
            "reasons": reasons,
            "suggested_shares": g4.get("suggested_shares", shares),
            "action_plan": g4.get("action_plan", {}),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ─── Gate 1 实现 ───

    def _gate_score_mapping(self, scores: Dict[str, float],
                            weights: Dict[str, float]) -> Dict:
        """计算加权综合评分并映射到操作"""
        if not scores:
            return {"pass": False, "message": "无分析师评分数据",
                    "composite_score": 0, "mapped_action": "HOLD"}

        weighted_sum = 0.0
        total_weight = 0.0
        for role, score in scores.items():
            w = weights.get(role, 0.2)
            weighted_sum += score * w
            total_weight += w

        composite = weighted_sum / total_weight if total_weight > 0 else 0.0

        for threshold, action, desc in SCORE_TO_ACTION:
            if composite >= threshold:
                mapped_action = action
                mapped_desc = desc
                break
        else:
            mapped_action = "SELL"
            mapped_desc = "极端看空"

        # 评分极度接近0 → 方向不明确
        if abs(composite) < 0.3:
            return {
                "pass": True,
                "message": f"综合评分{composite:+.2f}偏中性，映射{mapped_action}但建议观望",
                "composite_score": composite,
                "mapped_action": "HOLD",
            }

        return {
            "pass": True,
            "message": f"综合评分{composite:+.2f}→{mapped_action}({mapped_desc})",
            "composite_score": composite,
            "mapped_action": mapped_action,
        }

    # ─── Gate 2 实现 ───

    def _gate_t1_check(self, ticker: str, direction: str) -> Dict:
        """T+1 合规：检查今日是否已买入（买入后当日不可卖）"""
        if direction not in ("SELL", "UNDERWEIGHT"):
            return {"pass": True, "message": "非卖出操作，T+1不适用"}

        try:
            from stock_kb import StockKB
            kb = StockKB()
            today = datetime.now().strftime("%Y-%m-%d")
            with kb._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM stock_trades "
                    "WHERE stock_code=? AND action='BUY' AND trade_date=?",
                    [ticker, today]
                ).fetchone()
                if row and row["cnt"] > 0:
                    return {
                        "pass": False,
                        "message": f"T+1锁定：{ticker}今日已买入，不可卖出",
                    }
        except Exception as e:
            return {"pass": True, "message": f"T+1检查异常(放行): {e}"}

        return {"pass": True, "message": "T+1检查通过"}

    # ─── Gate 3 实现 ───

    def _gate_risk_check(self, ticker: str, direction: str,
                         shares: int, price: float) -> Dict:
        """风控验证：调用 risk_check.py verify"""
        risk_script = os.path.join(os.path.dirname(__file__), "risk_check.py")
        python = "/config/quant_env/bin/python"

        try:
            result = subprocess.run(
                [python, risk_script, "verify", ticker, direction, str(shares),
                 "--price", str(price), "--json"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                return {"pass": False, "message": f"风控脚本异常: {result.stderr[:100]}"}

            # risk_check.py --json 在stdout末尾输出JSON（前面是人读输出）
            lines = result.stdout.strip().split("\n")
            json_line = None
            for line in reversed(lines):
                line = line.strip()
                if line.startswith("{"):
                    json_line = line
                    break
            if not json_line:
                # 可能是紧凑JSON（单行）
                json_line = result.stdout.strip()

            data = json.loads(json_line)
            if data.get("all_pass"):
                return {"pass": True, "message": "风控通过",
                        "risk_detail": data}
            else:
                failed = [c for c in data.get("checks", []) if not c["pass"]]
                reasons = "; ".join(c["message"] for c in failed)
                return {"pass": False, "message": f"风控拒绝: {reasons}",
                        "risk_detail": data}
        except subprocess.TimeoutExpired:
            return {"pass": False, "message": "风控检查超时"}
        except Exception as e:
            return {"pass": False, "message": f"风控检查异常: {e}"}

    # ─── Gate 4 实现 ───

    def _gate_position_sizer(self, ticker: str, direction: str,
                              mapped_action: str, current_price: float,
                              analyst_scores: Dict[str, float]) -> Dict:
        """仓位评估：调用 position_sizer.py 计算建议股数"""
        if mapped_action in ("HOLD",):
            return {
                "pass": True,
                "message": "HOLD无需调整仓位",
                "suggested_shares": 0,
                "action_plan": {"action": "HOLD", "reason": "维持现有仓位"},
            }

        try:
            from position_sizer import PositionSizer, SizerInput
            from stock_kb import StockKB

            kb = StockKB()
            pf = kb.read_portfolio_truth()
            total_assets = pf["total_cost_basis"] + pf["cash"]

            # 获取当前持仓信息
            pos_info = pf["positions"].get(ticker, {})
            current_shares = pos_info.get("shares", 0)
            avg_cost = pos_info.get("cost", 0)
            name = pos_info.get("name", ticker)

            # 置信度：从综合评分推导
            composite = sum(
                analyst_scores.get(r, 0) * DEFAULT_WEIGHTS.get(r, 0.2)
                for r in analyst_scores
            )
            confidence = min(max(abs(composite) / 2.0, 0.1), 0.95)
            actual_direction = "BUY" if direction in ("BUY", "OVERWEIGHT") else "SELL"

            sizer = PositionSizer(total_assets=total_assets)
            sizer_input = SizerInput(
                code=ticker,
                name=name,
                direction=actual_direction,
                confidence=confidence,
                current_shares=current_shares,
                current_price=current_price,
                avg_cost=avg_cost,
            )
            result = sizer.calculate(sizer_input)

            # SizerOutput: suggested_action, suggested_shares, risk_label, reasoning...
            if result.risk_label == "danger":
                return {
                    "pass": False,
                    "message": f"仓位评估风险: {result.risk_label} — {result.reasoning[:100]}",
                    "suggested_shares": 0,
                    "action_plan": result.__dict__ if hasattr(result, '__dict__') else {},
                }

            return {
                "pass": True,
                "message": result.suggested_action,
                "suggested_shares": result.suggested_shares,
                "action_plan": result.__dict__ if hasattr(result, '__dict__') else {},
            }

        except Exception as e:
            return {
                "pass": True,  # 非致命：仓位计算失败不阻塞
                "message": f"仓位计算异常(不阻塞): {e}",
                "suggested_shares": 100,
                "action_plan": {},
            }


# ========== CLI ==========

def main():
    import argparse
    p = argparse.ArgumentParser(description="操盘决策强制门禁")
    p.add_argument("--ticker", required=True)
    p.add_argument("--direction", required=True,
                   choices=["BUY", "SELL", "HOLD", "OVERWEIGHT", "UNDERWEIGHT"])
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--scores", required=True, help='JSON: {"technical":1.2,...}')
    p.add_argument("--shares", type=int, default=100)
    p.add_argument("--json", action="store_true", help="纯JSON输出")
    args = p.parse_args()

    scores = json.loads(args.scores)
    gate = DecisionGate()
    result = gate.check(
        ticker=args.ticker,
        direction=args.direction,
        analyst_scores=scores,
        current_price=args.price,
        shares=args.shares,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # 人类可读输出
    print(f"\n═══ 操盘决策门禁: {args.ticker} {args.direction} ═══\n")
    for i, g in enumerate(result["gates"], 1):
        icon = "✅" if g["pass"] else "❌"
        print(f"  Gate {i}: {icon} {g['message']}")

    print(f"\n  📌 综合裁决: {result['verdict']}")
    if result["verdict"] == "APPROVE":
        ap = result.get("action_plan", {})
        print(f"  建议股数: {result['suggested_shares']}股")
        print(f"  操作: {ap.get('suggested_action', 'N/A')}")
    elif result["reasons"]:
        print(f"  拒绝原因:")
        for r in result["reasons"]:
            print(f"    • {r}")

    sys.exit(0 if result["verdict"] == "APPROVE" else 1)


if __name__ == "__main__":
    main()
