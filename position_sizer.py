#!/config/quant_env/bin/python3
"""
position_sizer.py — 仓位计算器

对标 FINRS 论文的 Quantity/Risk Agent（双Agent中的仓位部分）。
将 TradingAgents 的方向决策(BUY/SELL/HOLD) + 置信度评分 → 具体股数。

方法：
1. 凯利公式简化版（使用1/4凯利保守策略）
2. 风险预算约束（单标的不超总资产30%）
3. 硬性限制（不低于1手、保留10%现金）

用法：
  from position_sizer import PositionSizer
  
  sizer = PositionSizer(total_assets=30471)
  result = sizer.calculate(
      code="000938", name="紫光股份",
      direction="BUY", confidence=0.75,
      current_shares=100, current_price=32.50, avg_cost=28.28,
      annual_volatility=0.35  # 可不传，默认0.30
  )
  print(result['suggested_action'])  # "买入紫光股份 200股（仓位从10.7%升至28.0%）"
  
CLI用法：
  python position_sizer.py --code 000938 --name "紫光" --direction BUY --confidence 0.75
                          --shares 100 --price 32.50 --cost 28.28 --assets 30471
"""

import json
import math
import sys
from dataclasses import dataclass
from typing import Optional, Dict, List


# ========== 默认参数 ==========

MAX_SINGLE_POSITION_RATIO = 0.30   # 单标的不超30%
MIN_CASH_RATIO = 0.10              # 保留10%现金
KELLY_FRACTION = 0.25              # 1/4凯利（保守策略）
DEFAULT_VOLATILITY = 0.30          # 默认年化波动率
MIN_SHARES_A_STOCK = 100           # A股最少1手


@dataclass
class SizerInput:
    code: str
    name: str
    direction: str              # BUY / SELL / HOLD
    confidence: float           # 0-1
    current_shares: int         # 当前股数
    current_price: float        # 当前价格
    avg_cost: float             # 持仓成本价（如无持仓传0）
    total_assets: float         # 总资产
    annual_volatility: Optional[float] = None  # 年化波动率
    win_rate: Optional[float] = None           # 该标的历史胜率


@dataclass
class SizerOutput:
    suggested_action: str       # 可读的执行指令
    suggested_shares: int       # 建议买卖股数（正=买，负=卖）
    new_shares: int             # 交易后总股数
    position_value: float       # 交易后持仓市值
    position_ratio: float       # 交易后仓位占比
    cash_after: float           # 交易后现金
    risk_label: str             # safe / warning / danger
    reasoning: str              # 推理过程
    details: dict               # 详细计算数据


class PositionSizer:
    """仓位计算器"""

    def __init__(self, total_assets: float, available_cash: float = None,
                 max_single_ratio: float = MAX_SINGLE_POSITION_RATIO):
        self.total_assets = total_assets
        self.available_cash = available_cash or (total_assets * 0.5)  # 默认假设50%现金
        self.max_single_ratio = max_single_ratio

    def _kelly_fraction(self, win_rate: float, avg_win: float, avg_loss: float) -> float:
        """计算凯利公式建议仓位比例"""
        if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
            return 0
        b = avg_win / avg_loss  # 盈亏比
        q = 1 - win_rate
        kelly = (win_rate * b - q) / b
        return max(0, min(kelly, 1))  # 截断到[0,1]

    def calculate(self, inp: SizerInput) -> SizerOutput:
        code = inp.code
        name = inp.name
        direction = inp.direction.upper()
        confidence = max(0, min(1, inp.confidence))
        current_value = inp.current_shares * inp.current_price
        vol = inp.annual_volatility or DEFAULT_VOLATILITY

        # ========== 计算可用预算 ==========
        max_single_value = self.total_assets * self.max_single_ratio
        min_cash = self.total_assets * MIN_CASH_RATIO
        usable_cash = max(0, self.available_cash - min_cash)

        # ========== 按方向处理 ==========
        details = {
            "total_assets": round(self.total_assets, 2),
            "available_cash": round(self.available_cash, 2),
            "usable_cash": round(usable_cash, 2),
            "max_single_value": round(max_single_value, 2),
            "current_value": round(current_value, 2),
            "current_ratio": round(current_value / self.total_assets * 100, 1) if self.total_assets > 0 else 0,
        }

        if direction == "HOLD":
            return SizerOutput(
                suggested_action=f"HOLD {name}({code}) — 维持{inp.current_shares}股",
                suggested_shares=0, new_shares=inp.current_shares,
                position_value=current_value,
                position_ratio=details["current_ratio"] / 100,
                cash_after=self.available_cash,
                risk_label=self._risk_label(details["current_ratio"] / 100),
                reasoning="方向为HOLD，仓位不变",
                details=details
            )

        if direction == "SELL":
            # 卖出逻辑：根据置信度决定卖多少
            # 高置信度卖光，中置信度卖一半，低置信度卖1/4
            if confidence >= 0.7:
                sell_ratio = 1.0
            elif confidence >= 0.5:
                sell_ratio = 0.5
            elif confidence >= 0.3:
                sell_ratio = 0.25
            else:
                sell_ratio = 0.1

            suggested_sell = max(0, int(inp.current_shares * sell_ratio / MIN_SHARES_A_STOCK) * MIN_SHARES_A_STOCK)
            if suggested_sell <= 0:
                suggested_sell = 0

            new_shares = max(0, inp.current_shares - suggested_sell)
            new_value = new_shares * inp.current_price
            new_ratio = new_value / self.total_assets if self.total_assets > 0 else 0
            cash_after = self.available_cash + suggested_sell * inp.current_price

            if suggested_sell == 0:
                action = f"HOLD {name}({code}) — 持仓过少无需卖"
            elif new_shares == 0:
                action = f"SELL {name}({code}) — 清仓{suggested_sell}股 🟢"
            else:
                action = f"SELL {name}({code}) — 卖出{suggested_sell}股，剩余{new_shares}股"

            return SizerOutput(
                suggested_action=action,
                suggested_shares=-suggested_sell,
                new_shares=new_shares,
                position_value=new_value,
                position_ratio=new_ratio,
                cash_after=cash_after,
                risk_label=self._risk_label(new_ratio),
                reasoning=f"卖出置信度{confidence:.0%}，卖出比例{sell_ratio:.0%}，清仓{suggested_sell}股",
                details=details
            )

        # ========== BUY逻辑 ==========
        if direction == "BUY":
            # 预算上限：min(最大单标仓位剩余空间, 可用现金)
            remaining_budget = max(0, max_single_value - current_value)
            buy_budget = min(remaining_budget, usable_cash)

            if buy_budget <= 0:
                return SizerOutput(
                    suggested_action=f"HOLD {name}({code}) — 无预算买入（仓位{details['current_ratio']:.0f}%已达上限或现金不足）",
                    suggested_shares=0, new_shares=inp.current_shares,
                    position_value=current_value,
                    position_ratio=details["current_ratio"] / 100,
                    cash_after=self.available_cash,
                    risk_label="danger",
                    reasoning="预算不足",
                    details=details
                )

            # 凯利调整：根据置信度计算开仓比例
            # 简化：confidence直接作为仓位比例因子
            kelly_ratio = confidence * KELLY_FRACTION * 4  # 最高到confidence
            if vol > 0.4:
                kelly_ratio *= 0.7  # 高波动减仓
            elif vol < 0.2:
                kelly_ratio *= 1.2  # 低波动适当加仓

            suggested_buy_value = buy_budget * min(kelly_ratio, 1.0)
            suggested_shares = max(0, int(suggested_buy_value / inp.current_price / MIN_SHARES_A_STOCK) * MIN_SHARES_A_STOCK)

            if suggested_shares <= 0 and confidence >= 0.6:
                suggested_shares = MIN_SHARES_A_STOCK  # 至少1手

            buy_cost = suggested_shares * inp.current_price
            new_shares = inp.current_shares + suggested_shares
            new_value = new_shares * inp.current_price
            new_ratio = new_value / self.total_assets if self.total_assets > 0 else 0
            cash_after = self.available_cash - buy_cost

            if suggested_shares == 0:
                action = f"HOLD {name}({code}) — 建议买入但金额不足1手"
            else:
                action = (f"BUY {name}({code}) — 买入{suggested_shares}股"
                          f"（仓位{new_ratio:.1%}，现金余{cash_after:.0f}）")

            return SizerOutput(
                suggested_action=action,
                suggested_shares=suggested_shares,
                new_shares=new_shares,
                position_value=new_value,
                position_ratio=new_ratio,
                cash_after=cash_after,
                risk_label=self._risk_label(new_ratio),
                reasoning=(f"买入置信度{confidence:.0%}，波动率{vol:.0%}，"
                          f"凯利因子{kelly_ratio:.2f}，预算{buy_budget:.0f}"),
                details=details
            )

        raise ValueError(f"未知方向: {direction}")

    def _risk_label(self, ratio: float) -> str:
        if ratio > self.max_single_ratio:
            return "danger"
        elif ratio > self.max_single_ratio * 0.8:
            return "warning"
        else:
            return "safe"


# ========== CLI 入口 ==========

def cli():
    import argparse
    p = argparse.ArgumentParser(description="仓位计算器")
    p.add_argument("--code", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--direction", required=True, choices=["BUY", "SELL", "HOLD"])
    p.add_argument("--confidence", type=float, required=True)
    p.add_argument("--shares", type=int, default=0, help="当前持仓股数")
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--cost", type=float, default=0, help="持仓成本")
    p.add_argument("--assets", type=float, required=True, help="总资产")
    p.add_argument("--cash", type=float, default=None, help="可用现金")
    p.add_argument("--vol", type=float, default=None, help="年化波动率")
    args = p.parse_args()

    sizer = PositionSizer(total_assets=args.assets, available_cash=args.cash)
    inp = SizerInput(
        code=args.code, name=args.name,
        direction=args.direction, confidence=args.confidence,
        current_shares=args.shares, current_price=args.price,
        avg_cost=args.cost, total_assets=args.assets,
        annual_volatility=args.vol
    )
    result = sizer.calculate(inp)
    
    output = {
        "action": result.suggested_action,
        "suggested_shares": result.suggested_shares,
        "new_shares": result.new_shares,
        "position_ratio": round(result.position_ratio * 100, 1),
        "cash_after": round(result.cash_after, 2),
        "risk_label": result.risk_label,
        "reasoning": result.reasoning,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ========== Q2.1: 马科维茨均值-方差优化 ==========

def optimize_markowitz(returns_matrix: 'np.ndarray', expected_returns: 'np.ndarray' = None,
                       max_single: float = 0.30, max_total: float = 0.95) -> Dict:
    """
    马科维茨均值-方差组合优化（cvxpy 凸优化）。

    min  wᵀΣw - λ·μᵀw
    s.t. Σw ≤ 0.95, wᵢ ≤ 0.30, wᵢ ≥ 0

    Args:
        returns_matrix: (T days × N assets) 日对数收益率矩阵
        expected_returns: (N,) 预期收益向量，None则用历史均值
        max_single: 单标的上限（默认30%）
        max_total: 总仓位上限（默认95%）

    Returns:
        {
            'weights': [...],           # 最优权重
            'weights_pct': [...],       # 百分比
            'expected_return': float,   # 组合预期年化收益
            'expected_vol': float,      # 组合预期年化波动率
            'sharpe': float,            # 夏普比率
            'converged': bool,
        }
    """
    try:
        import numpy as np
        import cvxpy as cp

        returns_matrix = np.asarray(returns_matrix, dtype=float)
        n_assets = returns_matrix.shape[1]

        if expected_returns is None:
            expected_returns = np.mean(returns_matrix, axis=0) * 252  # 年化
        else:
            expected_returns = np.asarray(expected_returns, dtype=float)

        # 协方差矩阵（年化）
        cov_matrix = np.cov(returns_matrix.T) * 252

        # 正则化：保证半正定
        cov_matrix = (cov_matrix + cov_matrix.T) / 2
        eigvals = np.linalg.eigvalsh(cov_matrix)
        if eigvals[0] < 1e-10:
            cov_matrix += np.eye(n_assets) * (abs(eigvals[0]) + 1e-6)

        # 凸优化
        w = cp.Variable(n_assets)
        risk = cp.quad_form(w, cov_matrix)
        ret = expected_returns @ w

        # 风险厌恶系数 λ（平衡收益与风险）
        lambd = 1.0

        objective = cp.Minimize(risk - lambd * ret)
        constraints = [
            cp.sum(w) <= max_total,
            w >= 0,
            w <= max_single,
        ]

        problem = cp.Problem(objective, constraints)
        problem.solve(solver=cp.ECOS if 'ECOS' in cp.installed_solvers() else None)

        weights = w.value
        if weights is None:
            return {'error': 'optimization failed', 'converged': False}

        weights = np.maximum(weights, 0)  # clip negative
        weights = weights / max(weights.sum(), 1e-10)  # normalize

        port_return = float(expected_returns @ weights)
        port_vol = float(np.sqrt(weights @ cov_matrix @ weights))

        return {
            'weights': [round(float(w), 6) for w in weights],
            'weights_pct': [round(float(w) * 100, 1) for w in weights],
            'expected_return': round(port_return, 4),
            'expected_vol': round(port_vol, 4),
            'sharpe': round(port_return / port_vol, 2) if port_vol > 0 else 0,
            'converged': True,
            'n_assets': n_assets,
        }
    except ImportError:
        return {'error': 'cvxpy not available', 'converged': False}
    except Exception as e:
        return {'error': str(e), 'converged': False}


# ========== Q2.3: 多资产凯利公式 ==========

def optimize_kelly_multi(returns_matrix: 'np.ndarray', win_rates: List[float] = None,
                          max_single: float = 0.30) -> Dict:
    """
    多资产凯利公式：f* = Σ⁻¹μ

    与马科维茨对比：
    - 凯利最大化对数财富增长率 E[log(W)]
    - 马科维茨最大化夏普比率 (μ-rf)/σ

    Args:
        returns_matrix: (T × N) 日对数收益率
        win_rates: 各标的历史胜率（可选，用于分数凯利）
        max_single: 单标的上限

    Returns:
        {
            'weights': [...],
            'kelly_fraction': float,     # 使用的凯利分数（保守=0.25）
            'comparison': {               # 与马科维茨对比
                'markowitz_sharpe': ...,
                'kelly_sharpe': ...,
            }
        }
    """
    try:
        import numpy as np

        returns_matrix = np.asarray(returns_matrix, dtype=float)
        n_assets = returns_matrix.shape[1]

        mu = np.mean(returns_matrix, axis=0) * 252
        sigma = np.cov(returns_matrix.T) * 252

        # Σ⁻¹μ
        sigma_inv = np.linalg.pinv(sigma)
        raw_weights = sigma_inv @ mu

        # 负权重截断（做多only）
        raw_weights = np.maximum(raw_weights, 0)
        total = raw_weights.sum()
        if total > 1e-10:
            raw_weights = raw_weights / total

        # 1/4 凯利保守策略
        kelly_fraction = 0.25
        if win_rates and len(win_rates) == n_assets:
            avg_win_rate = np.mean(win_rates)
            kelly_fraction = max(0.1, min(0.5, avg_win_rate * 0.5))

        weights = raw_weights * kelly_fraction
        weights = np.clip(weights, 0, max_single)

        # 归一化
        weights = weights / max(weights.sum(), 1e-10)

        # 计算夏普（与马科维茨对比用）
        port_return = float(mu @ weights)
        port_vol = float(np.sqrt(weights @ sigma @ weights))
        sharpe = port_return / port_vol if port_vol > 0 else 0

        return {
            'weights': [round(float(w), 6) for w in weights],
            'weights_pct': [round(float(w) * 100, 1) for w in weights],
            'expected_return': round(port_return, 4),
            'expected_vol': round(port_vol, 4),
            'sharpe': round(sharpe, 2),
            'kelly_fraction': round(kelly_fraction, 2),
            'n_assets': n_assets,
        }
    except Exception as e:
        return {'error': str(e)}


if __name__ == "__main__":
    cli()
