#!/config/quant_env/bin/python3
"""
stock_kb.py — 股票知识库

为每只跟踪过的股票维护完整档案：
- 基本特征（波动率、行业、风格）
- 历史交易（每次买卖的时间/价格/理由/盈亏）
- 经验教训（成功模式/失败模式/关键洞察）
- 策略偏好（什么策略有效/什么策略无效）
- 注意力层级（当前关注程度）

用法:
  from stock_kb import StockKB
  
  kb = StockKB()
  
  # 记录交易
  kb.record_trade("000938", "BUY", 31.04, 200, "开盘恐慌砸到30.74后接回，情绪底")
  
  # 添加洞察
  kb.add_insight("000938", "强赛道(AI/信创)+弱质量(净利率2.24%)=需设硬止损")
  
  # 获取注意力列表
  active = kb.get_active_positions()      # 当前持仓
  monitoring = kb.get_monitoring_list()    # 需要关注的全部标的
  
  # 标记清仓（不删除）
  kb.mark_sold("518880", "黄金短期承压，清仓避险，等待低吸机会")

数据源: trade_log.db 中的 stock_kb / stock_trades / stock_insights 三张表
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = "/config/quant_scripts/trade_log.db"

# ========== 注意力层级 ==========

class AttentionLevel:
    ACTIVE_POSITION     = 3   # 当前持仓，最高优先级
    ACTIVE_MONITORING   = 2   # 曾持有/高关注，等待重新入场
    PASSIVE_MONITORING  = 1   # 自选池，有研究但未持仓
    DORMANT             = 0   # 曾经跟踪，当前不关注

# ========== 股票知识库主类 ==========

class StockKB:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._ensure_tables()
    
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    
    def _ensure_tables(self):
        with self._conn() as conn:
            # ---- 标的主表 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_kb (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    industry TEXT DEFAULT '',
                    sector TEXT DEFAULT '',
                    
                    -- 注意力
                    attention_level INTEGER DEFAULT 1,
                    attention_reason TEXT DEFAULT '',
                    
                    -- 特征（随时间积累发现）
                    volatility_level TEXT DEFAULT '',       -- high/medium/low
                    typical_daily_range_pct REAL,
                    beta REAL,
                    characteristics TEXT DEFAULT '{}',      -- JSON: {"高开低走概率":"60%", ...}
                    
                    -- 策略偏好
                    preferred_strategy TEXT DEFAULT '',     -- 最合适的策略
                    unsuitable_strategies TEXT DEFAULT '[]', -- 不适合的策略
                    optimal_holding_period TEXT DEFAULT '', -- swing/position/day_trade
                    
                    -- 风控参数（从经验中学习）
                    hard_stop_loss_pct REAL,
                    target_profit_pct REAL,
                    max_position_pct REAL,                  -- 单标的最大仓位占比
                    
                    -- 累计统计
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    total_buy_amount REAL DEFAULT 0,
                    total_sell_amount REAL DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    best_trade_pnl REAL DEFAULT 0,
                    worst_trade_pnl REAL DEFAULT 0,
                    
                    -- 当前持仓快照（冗余，方便快速查询）
                    current_shares INTEGER DEFAULT 0,
                    avg_cost REAL DEFAULT 0,
                    
                    -- 元数据
                    first_tracked_at TEXT NOT NULL,
                    last_traded_at TEXT,
                    last_reviewed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                )
            """)
            
            # ---- 交易记录 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    action TEXT NOT NULL,            -- BUY / SELL
                    price REAL NOT NULL,
                    shares INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    
                    -- 平仓时才有
                    pnl REAL,
                    pnl_pct REAL,
                    holding_days INTEGER,
                    
                    -- 决策记录
                    rationale TEXT DEFAULT '',
                    decision_process TEXT DEFAULT '', -- 'manual' / 'trading_agents' / 'risk_control'
                    signal_source TEXT DEFAULT '',    -- 信号来源
                    
                    -- 后续验证
                    was_good_decision INTEGER,        -- 1=正确 0=错误 NULL=未评估
                    lessons TEXT DEFAULT '',
                    
                    -- 市场环境
                    market_condition TEXT DEFAULT '', -- bull/bear/sideways/volatile
                    market_index_level TEXT DEFAULT '', -- 当时大盘点位
                    
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY (stock_code) REFERENCES stock_kb(code)
                )
            """)
            
            # ---- 洞察积累 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT NOT NULL,
                    insight_date TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    confidence TEXT DEFAULT 'medium',
                    source TEXT DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY (stock_code) REFERENCES stock_kb(code)
                )
            """)
            
            # ---- TradingAgents 分析报告缓存 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS analyst_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    
                    technical_score REAL,
                    sentiment_score REAL,
                    news_score REAL,
                    fundamental_score REAL,
                    composite_score REAL,
                    
                    verdict TEXT NOT NULL,
                    price_at_analysis REAL,
                    analyst_count INTEGER DEFAULT 4,
                    
                    technical_summary TEXT DEFAULT '',
                    sentiment_summary TEXT DEFAULT '',
                    news_summary TEXT DEFAULT '',
                    fundamental_summary TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    
                    token_cost_estimate REAL DEFAULT 0,
                    source TEXT DEFAULT 'trading_agents',
                    invalidated INTEGER DEFAULT 0,
                    
                    FOREIGN KEY (stock_code) REFERENCES stock_kb(code)
                )
            """)
            
            # 索引
            conn.execute("CREATE INDEX IF NOT EXISTS idx_skb_code ON stock_kb(code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_skb_attention ON stock_kb(attention_level)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_st_code ON stock_trades(stock_code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_st_date ON stock_trades(trade_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_si_code ON stock_insights(stock_code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ar_code ON analyst_reports(stock_code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ar_created ON analyst_reports(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ar_verdict ON analyst_reports(verdict)")
    
    # ========== 标的管理 ==========
    
    def ensure_stock(self, code: str, name: str = "", industry: str = "", 
                     attention: int = AttentionLevel.PASSIVE_MONITORING,
                     reason: str = "") -> int:
        """确保标的存在，不存在则创建。返回 id"""
        with self._conn() as conn:
            row = conn.execute("SELECT id FROM stock_kb WHERE code=?", [code]).fetchone()
            if row:
                # 已存在，仅更新名称（如果提供了）
                if name:
                    conn.execute(
                        "UPDATE stock_kb SET name=?, updated_at=datetime('now','localtime') WHERE code=?",
                        [name, code]
                    )
                return row["id"]
            
            now = datetime.now().isoformat()
            cur = conn.execute("""
                INSERT INTO stock_kb (code, name, industry, attention_level, attention_reason, 
                                     first_tracked_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [code, name, industry, attention, reason, now, now, now])
            return cur.lastrowid
    
    def update_attention(self, code: str, level: int, reason: str = ""):
        """更新注意力层级"""
        with self._conn() as conn:
            conn.execute("""
                UPDATE stock_kb SET attention_level=?, attention_reason=?, 
                updated_at=datetime('now','localtime')
                WHERE code=?
            """, [level, reason, code])
    
    def update_position_snapshot(self, code: str, shares: int, avg_cost: float):
        """更新当前持仓快照"""
        with self._conn() as conn:
            conn.execute("""
                UPDATE stock_kb SET current_shares=?, avg_cost=?, 
                attention_level=?, updated_at=datetime('now','localtime')
                WHERE code=?
            """, [shares, avg_cost, 
                  AttentionLevel.ACTIVE_POSITION if shares > 0 else AttentionLevel.ACTIVE_MONITORING,
                  code])
    
    def mark_sold(self, code: str, reason: str = "", keep_monitoring: bool = True):
        """标记清仓（不删除记录，保持知识积累）"""
        level = AttentionLevel.ACTIVE_MONITORING if keep_monitoring else AttentionLevel.DORMANT
        with self._conn() as conn:
            conn.execute("""
                UPDATE stock_kb SET current_shares=0, avg_cost=0,
                attention_level=?, attention_reason=?,
                updated_at=datetime('now','localtime')
                WHERE code=?
            """, [level, reason, code])
    
    def get_stock(self, code: str) -> dict:
        """获取单只股票完整档案"""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM stock_kb WHERE code=?", [code]).fetchone()
            if not row:
                return None
            d = dict(row)
            try: d["characteristics"] = json.loads(d.get("characteristics", "{}"))
            except: d["characteristics"] = {}
            try: d["unsuitable_strategies"] = json.loads(d.get("unsuitable_strategies", "[]"))
            except: d["unsuitable_strategies"] = []
            return d
    
    def get_active_positions(self) -> list:
        """获取当前持仓"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM stock_kb WHERE current_shares > 0 ORDER BY attention_level DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    
    def get_monitoring_list(self, min_level: int = AttentionLevel.PASSIVE_MONITORING) -> list:
        """获取需要关注的标的列表（按注意力排序）"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT code, name, attention_level, attention_reason, 
                   current_shares, avg_cost, industry
                   FROM stock_kb WHERE attention_level >= ? 
                   ORDER BY attention_level DESC, last_traded_at DESC""",
                [min_level]
            ).fetchall()
        return [dict(r) for r in rows]
    
    def get_insights(self, code: str, category: str = None, limit: int = 30) -> list:
        """获取某个标的的经验教训"""
        with self._conn() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM stock_insights WHERE stock_code=? AND category=? ORDER BY insight_date DESC LIMIT ?",
                    [code, category, limit]
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM stock_insights WHERE stock_code=? ORDER BY insight_date DESC LIMIT ?",
                    [code, limit]
                ).fetchall()
        return [dict(r) for r in rows]
    
    # ========== 交易记录 ==========
    
    def record_trade(self, code: str, action: str, price: float, shares: int,
                     rationale: str = "", decision_process: str = "manual",
                     signal_source: str = "", market_condition: str = "",
                     pnl: float = None, pnl_pct: float = None, holding_days: int = None,
                     lessons: str = "") -> int:
        """记录一笔交易，自动更新标的状态和统计"""
        now = datetime.now()
        amount = price * shares
        
        with self._conn() as conn:
            # 写入交易记录
            cur = conn.execute("""
                INSERT INTO stock_trades (stock_code, trade_date, action, price, shares, amount,
                    pnl, pnl_pct, holding_days, rationale, decision_process, signal_source,
                    market_condition, lessons, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                code, now.strftime("%Y-%m-%d"), action, price, shares, amount,
                pnl, pnl_pct, holding_days, rationale, decision_process, signal_source,
                market_condition, lessons, now.isoformat()
            ])
            trade_id = cur.lastrowid
            
            # 更新标的主表统计
            if action == "BUY":
                # 计算新的平均成本
                stock = dict(conn.execute(
                    "SELECT current_shares, avg_cost, total_buy_amount, total_trades FROM stock_kb WHERE code=?",
                    [code]
                ).fetchone() or {"current_shares": 0, "avg_cost": 0, "total_buy_amount": 0, "total_trades": 0})
                
                old_shares = stock["current_shares"]
                old_cost = stock["avg_cost"]
                old_amount = old_shares * old_cost if old_shares > 0 else 0
                new_total = old_amount + amount
                new_shares = old_shares + shares
                new_cost = new_total / new_shares if new_shares > 0 else 0
                
                conn.execute("""
                    UPDATE stock_kb SET 
                        current_shares=?, avg_cost=?,
                        total_buy_amount = total_buy_amount + ?,
                        total_trades = total_trades + 1,
                        last_traded_at=?,
                        attention_level=?,
                        updated_at=datetime('now','localtime')
                    WHERE code=?
                """, [
                    new_shares, round(new_cost, 3),
                    amount, now.isoformat(),
                    AttentionLevel.ACTIVE_POSITION, code
                ])
            
            elif action == "SELL":
                stock = dict(conn.execute(
                    "SELECT current_shares, total_sell_amount, total_trades, winning_trades, "
                    "best_trade_pnl, worst_trade_pnl, total_pnl FROM stock_kb WHERE code=?",
                    [code]
                ).fetchone() or {"current_shares": 0, "total_sell_amount": 0, "total_trades": 0,
                                 "winning_trades": 0, "best_trade_pnl": 0, "worst_trade_pnl": 0, "total_pnl": 0})
                
                new_shares = max(0, stock["current_shares"] - shares)
                is_win = (pnl or 0) > 0
                new_best = max(stock["best_trade_pnl"] or 0, pnl or 0)
                new_worst = min(stock["worst_trade_pnl"] or 0, pnl or 0)
                
                conn.execute("""
                    UPDATE stock_kb SET 
                        current_shares=?,
                        total_sell_amount = total_sell_amount + ?,
                        total_trades = total_trades + 1,
                        winning_trades = winning_trades + ?,
                        best_trade_pnl=?, worst_trade_pnl=?,
                        total_pnl = total_pnl + ?,
                        last_traded_at=?,
                        attention_level = CASE WHEN current_shares - ? <= 0 THEN ? ELSE attention_level END,
                        updated_at=datetime('now','localtime')
                    WHERE code=?
                """, [
                    new_shares, amount,
                    1 if is_win else 0,
                    new_best, new_worst,
                    pnl or 0,
                    now.isoformat(),
                    shares, AttentionLevel.ACTIVE_MONITORING,
                    code
                ])
                
                # 如果彻底清仓
                if new_shares == 0:
                    conn.execute("UPDATE stock_kb SET avg_cost=0 WHERE code=?", [code])
            
            return trade_id
    
    # ========== 洞察管理 ==========
    
    def add_insight(self, code: str, content: str, category: str = "observation",
                    confidence: str = "medium", source: str = "") -> int:
        """添加一条经验教训"""
        now = datetime.now()
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO stock_insights (stock_code, insight_date, category, content, confidence, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [code, now.strftime("%Y-%m-%d"), category, content, confidence, source, now.isoformat()])
            return cur.lastrowid
    
    def update_characteristics(self, code: str, key: str, value):
        """更新标的特征（增量更新characteristics JSON）"""
        stock = self.get_stock(code)
        if not stock:
            return
        chars = stock.get("characteristics", {})
        chars[key] = value
        with self._conn() as conn:
            conn.execute(
                "UPDATE stock_kb SET characteristics=?, updated_at=datetime('now','localtime') WHERE code=?",
                [json.dumps(chars, ensure_ascii=False), code]
            )
    
    def evaluate_trade(self, trade_id: int, was_good: bool, lessons: str = ""):
        """事后评估一笔交易"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE stock_trades SET was_good_decision=?, lessons=? WHERE id=?",
                [1 if was_good else 0, lessons, trade_id]
            )
    
    # ========== TradingAgents 分析报告缓存 ==========
    
    def save_report(self, code: str, scores: dict, verdict: str, price: float,
                    summaries: dict = None, token_cost: float = 0) -> int:
        """保存一份 TradingAgents 分析报告
        scores: {technical, sentiment, news, fundamental, composite}
        summaries: {technical, sentiment, news, fundamental, summary}
        """
        now = datetime.now()
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO analyst_reports (
                    stock_code, created_at,
                    technical_score, sentiment_score, news_score, fundamental_score, composite_score,
                    verdict, price_at_analysis,
                    technical_summary, sentiment_summary, news_summary, fundamental_summary, summary,
                    token_cost_estimate, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                code, now.isoformat(),
                scores.get('technical'), scores.get('sentiment'), scores.get('news'),
                scores.get('fundamental'), scores.get('composite', 0),
                verdict, price,
                (summaries or {}).get('technical', ''),
                (summaries or {}).get('sentiment', ''),
                (summaries or {}).get('news', ''),
                (summaries or {}).get('fundamental', ''),
                (summaries or {}).get('summary', ''),
                token_cost, 'trading_agents'
            ])
            return cur.lastrowid
    
    def check_cache(self, code: str, current_price: float, 
                    max_age_hours: int = 4, max_price_change_pct: float = 3.0) -> dict:
        """检查是否有可复用的分析缓存
        返回: {hit: True/False, report: {...}, reason: str}
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM analyst_reports 
                   WHERE stock_code=? AND invalidated=0 
                   ORDER BY created_at DESC LIMIT 1""",
                [code]
            ).fetchone()
        
        if not row:
            return {"hit": False, "report": None, "reason": "无历史分析"}
        
        report = dict(row)
        created = datetime.fromisoformat(report['created_at'])
        age = (datetime.now() - created).total_seconds() / 3600
        
        if age > max_age_hours:
            return {"hit": False, "report": report, 
                    "reason": f"分析过期({age:.1f}h > {max_age_hours}h)"}
        
        if report['price_at_analysis'] and report['price_at_analysis'] > 0:
            price_change = abs(current_price - report['price_at_analysis']) / report['price_at_analysis'] * 100
            if price_change > max_price_change_pct:
                return {"hit": False, "report": report,
                        "reason": f"价格变动过大({price_change:.1f}% > {max_price_change_pct}%)"}
        else:
            price_change = 0
        
        return {
            "hit": True,
            "report": report,
            "reason": f"缓存有效(age={age:.1f}h, Δprice={price_change:.1f}%)"
        }
    
    def get_latest_report(self, code: str) -> dict:
        """获取最新一份分析报告"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM analyst_reports WHERE stock_code=? ORDER BY created_at DESC LIMIT 1",
                [code]
            ).fetchone()
        return dict(row) if row else None
    
    def invalidate_reports(self, code: str):
        """标记某标的所有分析报告为无效"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE analyst_reports SET invalidated=1 WHERE stock_code=?",
                [code]
            )
    
    # ========== 导出 ==========
    
    def export_guard_config(self) -> dict:
        """从 stock_kb 导出 guard_config.json 格式（positions + watch_list）"""
        positions = {}
        watch = {}
        price_alerts = {}
        
        for stock in self.get_monitoring_list():
            code = stock["code"]
            name = stock["name"]
            watch[code] = name
            
            if stock["current_shares"] > 0:
                positions[code] = {
                    "name": name,
                    "shares": stock["current_shares"],
                    "cost": stock["avg_cost"]
                }
        
        return {
            "positions": positions,
            "watch_list": watch,
            "price_alerts": price_alerts
        }
    
    def stats_summary(self) -> dict:
        """知识库统计概览"""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) as n FROM stock_kb").fetchone()["n"]
            active = conn.execute(
                "SELECT COUNT(*) as n FROM stock_kb WHERE current_shares > 0"
            ).fetchone()["n"]
            monitoring = conn.execute(
                "SELECT COUNT(*) as n FROM stock_kb WHERE attention_level >= 2"
            ).fetchone()["n"]
            total_insights = conn.execute(
                "SELECT COUNT(*) as n FROM stock_insights"
            ).fetchone()["n"]
            total_trades = conn.execute(
                "SELECT COUNT(*) as n FROM stock_trades"
            ).fetchone()["n"]
            
            # 总盈亏
            pnl_row = conn.execute(
                "SELECT SUM(pnl) as total_pnl FROM stock_trades WHERE pnl IS NOT NULL"
            ).fetchone()
            
            return {
                "total_stocks_tracked": total,
                "active_positions": active,
                "actively_monitoring": monitoring,
                "total_insights": total_insights,
                "total_trades_recorded": total_trades,
                "total_realized_pnl": round(pnl_row["total_pnl"] or 0, 2)
            }

    # ========== Cron 上下文 ==========

    def get_context_for_cron(self, max_insights_per_stock: int = 3) -> str:
        """生成 cron 任务用的紧凑上下文（节省 token）"""
        lines = []
        lines.append("## 📚 股票知识库上下文")
        
        # 当前持仓
        positions = self.get_active_positions()
        if positions:
            lines.append("\n### 🔴 当前持仓")
            for s in positions:
                lines.append(
                    f"- **{s['name']}**({s['code']}) {s['current_shares']}股 "
                    f"@成本{s['avg_cost']:.3f} | "
                    f"特性: {s.get('volatility_level','?')}波动"
                )
                if s.get('preferred_strategy'):
                    lines.append(f"  策略: {s['preferred_strategy']}")
                if s.get('hard_stop_loss_pct'):
                    lines.append(f"  硬止损: -{s['hard_stop_loss_pct']}%")
                if s.get('characteristics'):
                    try:
                        chars = json.loads(s['characteristics']) if isinstance(s['characteristics'], str) else s['characteristics']
                        for k, v in chars.items():
                            lines.append(f"  {k}: {v}")
                    except: pass
        
        # 关键洞察（只取高置信度的）
        lines.append("\n### 💡 关键经验（高置信度）")
        total_chars = 0
        max_chars = 1500
        for s in self.get_monitoring_list(min_level=2):
            insights = self.get_insights(s['code'], limit=max_insights_per_stock)
            high_insights = [i for i in insights if i.get('confidence') == 'high']
            if not high_insights:
                continue
            for i in high_insights:
                line = f"- [{s['name']}] {i['content']}"
                if total_chars + len(line) > max_chars:
                    break
                lines.append(line)
                total_chars += len(line)
        
        # 交易规则提醒
        lines.append("\n### 📋 硬性规则")
        rules = self._get_active_rules()
        for r in rules:
            lines.append(f"- {r}")
        
        return "\n".join(lines)

    def _get_active_rules(self) -> list:
        """获取当前活跃的交易规则"""
        rules = []
        # T+1 检查
        with self._conn() as conn:
            today_trades = conn.execute(
                "SELECT stock_code, action, shares FROM stock_trades WHERE trade_date=? AND action='BUY'",
                [datetime.now().strftime("%Y-%m-%d")]
            ).fetchall()
            for t in today_trades:
                name_row = conn.execute("SELECT name FROM stock_kb WHERE code=?", [t['stock_code']]).fetchone()
                name = name_row['name'] if name_row else t['stock_code']
                rules.append(f"⚠ T+1锁定: {name}({t['stock_code']}) 今日买入{t['shares']}股，今日不可卖出")
        
        # 从洞察中提取规则类
        rules_insights = self._conn().execute(
            "SELECT stock_code, content FROM stock_insights WHERE category='rule' ORDER BY insight_date DESC LIMIT 5"
        ).fetchall()
        for ri in rules_insights:
            rules.append(f"📏 {ri['content']}")
        
        return rules


# ========== CLI ==========

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: stock_kb.py <command> [args...]")
        print()
        print("查询命令（cron Agent 按需调用）：")
        print("  positions              — 当前持仓列表（紧凑）")
        print("  rules                  — T+1锁定 + 硬性规则")
        print("  insights <code>        — 指定标的的经验洞察")
        print("  search <keyword>       — 搜索洞察")
        print("  show <code>            — 单只标的完整档案")
        print("  cache-check <code> <price> — 检查分析缓存（价格变动<3%+<4h=命中）")
        print("  report-latest <code>   — 查看最新分析报告")
        print()
        print("管理命令：")
        print("  list [level]           — 注意力列表")
        print("  stats                  — 知识库统计")
        print("  export-config          — 导出 guard_config.json")
        print("  context                — 完整上下文（旧版，不推荐）")
        sys.exit(1)
    
    kb = StockKB()
    cmd = sys.argv[1]
    
    if cmd == "positions":
        positions = kb.get_active_positions()
        if not positions:
            print("(无当前持仓)")
        else:
            print(f"{'代码':8s} {'名称':10s} {'持仓':>6s} {'成本':>8s} {'波动':>4s}")
            print("-" * 46)
            for s in positions:
                vol = s.get('volatility_level', '?')
                print(f"{s['code']:8s} {s['name']:10s} {s['current_shares']:>4d}股 @{s['avg_cost']:>7.3f} {vol:>4s}")
            print()
            # 各标的策略概要
            for s in positions:
                if s.get('preferred_strategy'):
                    print(f"  {s['name']}: {s['preferred_strategy']}")
    
    elif cmd == "rules":
        rules = kb._get_active_rules()
        if not rules:
            print("(无活跃规则)")
        else:
            for r in rules:
                print(r)
    
    elif cmd == "insights":
        if len(sys.argv) < 3:
            print("Usage: stock_kb.py insights <code> [category]")
            print("Categories: observation, risk, strategy, lesson, rule")
            sys.exit(1)
        code = sys.argv[2]
        category = sys.argv[3] if len(sys.argv) > 3 else None
        
        stock = kb.get_stock(code)
        name = stock['name'] if stock else code
        insights = kb.get_insights(code, category=category, limit=10)
        
        if not insights:
            print(f"(无{name}的洞察记录)")
        else:
            cat_map = {"observation":"👁观察","risk":"⚠风险","strategy":"🎯策略","lesson":"📝教训","rule":"📋规则"}
            for i in insights:
                conf = "🔴" if i.get('confidence')=='high' else "🟡"
                cat = cat_map.get(i.get('category',''), i.get('category',''))
                print(f"{conf} [{cat}] {i['content']}")
    
    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: stock_kb.py search <keyword>")
            sys.exit(1)
        keyword = sys.argv[2]
        with kb._conn() as conn:
            rows = conn.execute(
                """SELECT si.*, sk.name FROM stock_insights si 
                   JOIN stock_kb sk ON si.stock_code = sk.code
                   WHERE si.content LIKE ? 
                   ORDER BY si.confidence DESC, si.insight_date DESC LIMIT 10""",
                [f"%{keyword}%"]
            ).fetchall()
        if not rows:
            print(f"(未找到包含'{keyword}'的洞察)")
        else:
            for r in rows:
                conf = "🔴" if r['confidence']=='high' else "🟡"
                print(f"{conf} [{r['name']}({r['stock_code']})] {r['content']}")
    
    elif cmd == "cache-check":
        if len(sys.argv) < 4:
            print("Usage: stock_kb.py cache-check <code> <current_price>")
            sys.exit(1)
        code = sys.argv[2]
        price = float(sys.argv[3])
        result = kb.check_cache(code, price)
        if result['hit']:
            r = result['report']
            print(f"✅ 缓存命中 | {result['reason']}")
            print(f"   {r['verdict']} @{r['price_at_analysis']:.2f} | "
                  f"技术{r['technical_score']:+.1f} 情绪{r['sentiment_score']:+.1f} "
                  f"新闻{r['news_score']:+.1f} 基本面{r['fundamental_score']:+.1f} "
                  f"→ 综合{r['composite_score']:+.1f}")
            if r['summary']:
                print(f"   {r['summary'][:120]}")
        else:
            print(f"❌ 缓存未命中 | {result['reason']}")
            if result['report']:
                r = result['report']
                print(f"   最近分析: {r['verdict']} @{r['price_at_analysis']:.2f} "
                      f"(综合{r['composite_score']:+.1f})")
    
    elif cmd == "report-latest":
        if len(sys.argv) < 3:
            print("Usage: stock_kb.py report-latest <code>")
            sys.exit(1)
        code = sys.argv[2]
        report = kb.get_latest_report(code)
        if not report:
            print(f"(无{code}的分析报告)")
        else:
            print(f"=== {code} 最新分析 ===")
            print(f"时间: {report['created_at'][:16]}")
            print(f"价格: {report['price_at_analysis']:.2f}")
            print(f"评分: 技术{report['technical_score']:+.1f} 情绪{report['sentiment_score']:+.1f} "
                  f"新闻{report['news_score']:+.1f} 基本面{report['fundamental_score']:+.1f} "
                  f"→ 综合{report['composite_score']:+.1f}")
            print(f"裁决: {report['verdict']}")
            if report['summary']:
                print(f"摘要: {report['summary']}")
            if report['invalidated']:
                print("⚠ 已标记为无效")
    
    elif cmd == "stats":
        stats = kb.stats_summary()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    
    elif cmd == "list":
        level = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        stocks = kb.get_monitoring_list(min_level=level)
        for s in stocks:
            pos = f"持仓{s['current_shares']}股@{s['avg_cost']:.2f}" if s['current_shares'] > 0 else "未持仓"
            print(f"  {s['code']} {s['name']:8s} | Lv{s['attention_level']} | {pos} | {s['attention_reason'][:40]}")
    
    elif cmd == "show":
        code = sys.argv[2]
        stock = kb.get_stock(code)
        if stock:
            print(json.dumps(stock, ensure_ascii=False, indent=2))
        else:
            print(f"Stock {code} not in knowledge base")
    
    elif cmd == "export-config":
        config = kb.export_guard_config()
        print(json.dumps(config, ensure_ascii=False, indent=2))
    
    elif cmd == "context":
        ctx = kb.get_context_for_cron()
        print(ctx)
    
    else:
        print(f"Unknown command: {cmd}")
