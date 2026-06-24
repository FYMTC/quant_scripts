"""
trade_db.py — 交易系统数据层

三类存储：
  1. SQLite (trade_log.db) — 追加型历史日志，支持按时间/类型/标的查询
  2. JSON (market_snapshot.json) — 最新行情快照，单文件覆盖写
  3. JSON (daily_plan.json) — 当日操盘计划，由盘前任务写入

用法：
  from trade_db import TradeDB, MarketSnapshot, DailyPlan

  # 写入快照（守护进程每轮调用）
  snap = MarketSnapshot()
  snap.update("002594", {"price": 101.5, "pct": -0.55, ...})
  snap.save()

  # 读取快照（所有cron任务调用）
  snap = MarketSnapshot()
  data = snap.get("002594")  # {"price": 101.5, ...} 或 None

  # 写入日志
  db = TradeDB()
  db.log("开盘快报", "002594", "比亚迪开101.8, 涨0.12%", {"price":101.8})
  db.log("异动", "600487", "大跌-4.16%", {"pct":-4.16})

  # 查询
  today_alerts = db.query(type="异动", date="2026-04-28")
  last_10 = db.query(limit=10)
"""

import sqlite3
import json
import os
from datetime import datetime, date
from system_config import cfg

# ========== 路径 ==========
DB_PATH = cfg.path.trade_db
SNAPSHOT_PATH = cfg.path.market_snapshot
PLAN_PATH = cfg.path.daily_plan

# ========== SQLite 操作日志 ==========

# ========== 信号日志表（Phase 1: 执行层骨架） ==========

class SignalLog:
    """信号生命周期管理 — 从生成到回测的全链路追踪"""

    SIGNAL_SOURCES = ["qlib", "trading_agents", "rd_agent"]
    SIGNAL_TYPES = ["BUY", "SELL", "HOLD", "OVERWEIGHT", "UNDERWEIGHT"]
    LIFECYCLE_STATUSES = [
        "pending",      # 刚生成，待模拟
        "active",       # 模拟中（已记录入场价和日期）
        "backtesting",  # 回测中
        "passed",       # 回测通过（胜率/夏普达标）
        "failed",       # 回测不通过
        "expired",      # 超过有效期未验证
        "executed",     # 已实盘执行
    ]

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._ensure_table()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_table(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    signal_type TEXT NOT NULL,
                    score REAL DEFAULT 0,
                    confidence REAL DEFAULT 0,
                    target_price REAL,
                    stop_loss REAL,
                    reason TEXT DEFAULT '',
                    detail TEXT DEFAULT '{}',

                    status TEXT DEFAULT 'pending',
                    sim_entry_price REAL,
                    sim_entry_date TEXT,
                    sim_exit_price REAL,
                    sim_exit_date TEXT,
                    sim_pnl REAL,
                    sim_pnl_pct REAL,

                    backtest_result TEXT DEFAULT '{}',
                    verified_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_status ON signal_log(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_source ON signal_log(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_code ON signal_log(code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_date ON signal_log(date)")

    def create(self, source: str, code: str, name: str, signal_type: str,
               score: float = 0, confidence: float = 0,
               target_price: float = None, stop_loss: float = None,
               reason: str = "", detail: dict = None) -> int:
        """生成一条信号，返回 signal_id"""
        now = datetime.now()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO signal_log 
                   (created_at, date, source, code, name, signal_type, score, confidence,
                    target_price, stop_loss, reason, detail, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    now.isoformat(),
                    now.strftime("%Y-%m-%d"),
                    source, code, name, signal_type,
                    score, confidence,
                    target_price, stop_loss,
                    reason,
                    json.dumps(detail or {}, ensure_ascii=False)
                )
            )
            return cur.lastrowid

    def update_status(self, signal_id: int, status: str, **kwargs):
        """更新信号状态和可选字段"""
        allowed = {
            "sim_entry_price", "sim_entry_date", "sim_exit_price",
            "sim_exit_date", "sim_pnl", "sim_pnl_pct",
            "backtest_result", "verified_at", "target_price", "stop_loss"
        }
        sets = ["status = ?"]
        params = [status]
        for k, v in kwargs.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(v if not isinstance(v, dict) else json.dumps(v, ensure_ascii=False))
        params.append(signal_id)

        with self._conn() as conn:
            conn.execute(
                f"UPDATE signal_log SET {', '.join(sets)} WHERE id = ?",
                params
            )

    def get_pending(self, source: str = None) -> list:
        """获取待模拟的信号"""
        with self._conn() as conn:
            if source:
                rows = conn.execute(
                    "SELECT * FROM signal_log WHERE status='pending' AND source=? ORDER BY created_at",
                    [source]
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM signal_log WHERE status='pending' ORDER BY created_at"
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_active(self) -> list:
        """获取模拟中的信号"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signal_log WHERE status='active' ORDER BY sim_entry_date"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def query(self, source: str = None, status: str = None, code: str = None,
              date: str = None, limit: int = 50) -> list:
        """通用查询"""
        conds, params = [], []
        if source: conds.append("source=?"); params.append(source)
        if status: conds.append("status=?"); params.append(status)
        if code: conds.append("code=?"); params.append(code)
        if date: conds.append("date=?"); params.append(date)

        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM signal_log {where} ORDER BY created_at DESC LIMIT ?",
                params + [limit]
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def stats_by_source(self, days: int = 30) -> dict:
        """按信号源统计准确率"""
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT source, 
                   COUNT(*) as total,
                   SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) as passed,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                   AVG(CASE WHEN sim_pnl IS NOT NULL THEN sim_pnl_pct ELSE NULL END) as avg_pnl,
                   AVG(score) as avg_score
                   FROM signal_log 
                   WHERE created_at >= ? AND status IN ('passed','failed')
                   GROUP BY source""",
                [cutoff]
            ).fetchall()
        results = {}
        for r in rows:
            total = r["total"] or 0
            passed = r["passed"] or 0
            results[r["source"]] = {
                "total": total,
                "passed": passed,
                "failed": r["failed"] or 0,
                "accuracy": round(passed / total * 100, 1) if total > 0 else 0,
                "avg_pnl_pct": round(r["avg_pnl"] or 0, 2),
                "avg_score": round(r["avg_score"] or 0, 1),
            }
        return results

    def expire_old_pending(self, max_days: int = 14):
        """过期未验证的信号标记为expired"""
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=max_days)).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE signal_log SET status='expired' WHERE status='pending' AND created_at < ?",
                [cutoff]
            )

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        try: d["detail"] = json.loads(d.get("detail", "{}"))
        except: d["detail"] = {}
        try: d["backtest_result"] = json.loads(d.get("backtest_result", "{}"))
        except: d["backtest_result"] = {}
        return d


# ========== SQLite 操作日志 ==========

class TradeDB:
    """交易操作日志 — SQLite 存储"""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._ensure_db()

    def _ensure_db(self):
        """建表（如果不存在）"""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trading_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    date TEXT NOT NULL,
                    type TEXT NOT NULL,
                    code TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    message TEXT DEFAULT '',
                    detail TEXT DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_journal_date ON trading_journal(date)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_journal_type ON trading_journal(type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_journal_code ON trading_journal(code)
            """)

    def _conn(self):
        """获取数据库连接（自动提交）"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # 写前日志模式，避免锁冲突
        return conn

    def log(self, log_type: str, code: str = "", name: str = "", message: str = "", detail: dict = None):
        """写入一条操作日志"""
        now = datetime.now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO trading_journal (time, date, type, code, name, message, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now.strftime("%H:%M:%S"),
                    now.strftime("%Y-%m-%d"),
                    log_type,
                    code,
                    name,
                    message,
                    json.dumps(detail or {}, ensure_ascii=False)
                )
            )

    def query(self, type: str = None, code: str = None, date: str = None,
              limit: int = 50, offset: int = 0):
        """查询日志"""
        conditions = []
        params = []

        if type:
            conditions.append("type = ?")
            params.append(type)
        if code:
            conditions.append("code = ?")
            params.append(code)
        if date:
            conditions.append("date = ?")
            params.append(date)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT time, date, type, code, name, message, detail "
                f"FROM trading_journal {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall()

        results = []
        for r in rows:
            item = dict(r)
            try:
                item["detail"] = json.loads(item["detail"])
            except:
                item["detail"] = {}
            results.append(item)
        return results

    def count(self, type: str = None, code: str = None, date: str = None) -> int:
        """统计记录数"""
        conditions = []
        params = []
        if type:
            conditions.append("type = ?")
            params.append(type)
        if code:
            conditions.append("code = ?")
            params.append(code)
        if date:
            conditions.append("date = ?")
            params.append(date)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM trading_journal {where}", params
            ).fetchone()
            return row["cnt"] if row else 0

    def delete_old(self, days: int = 90):
        """清理 N 天前的日志"""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM trading_journal WHERE date < ?",
                [(datetime.now() - __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d")]
            )


# ========== Cron报告持久化（DB上下文传递） ==========

class CronReport:
    """Cron任务报告持久化 —— 用DB替代context_from和memory传递上下文"""

    # H6: 当 LLM 落库使用「详见输出」等占位时，从 apps 管线 JSON 自动注水，保证可审计
    ARTIFACT_JSON_BY_REPORT_TYPE = {
        "morning": cfg.path.morning_output,
        "flash_open": cfg.path.flash_output,
        "midday_flash": cfg.path.midday_output,
        "midday": cfg.path.noon_output,
        "afternoon": cfg.path.afternoon_output,
        "close": cfg.path.close_output,
        "night": cfg.path.night_output,
        "weekly": cfg.path.weekend_data,
    }
    _PLACEHOLDER_MARKERS = ("详见输出", "见输出", "详见上文", "略", "从略", "完整分析略")

    REPORT_TYPES = {
        "盘前简报": "morning",
        "开盘闪电战": "flash_open",
        "盘中快照 10:00": "midday_flash",   # P1-2 修复: 新增
        "午间总结": "midday", 
        "下午速报": "afternoon",
        "收盘总结": "close",
        "夜报": "night",
        "周末周报": "weekly",
    }

    # 每个cron启动时需要的前置报告（用report_type做key）
    CONTEXT_CHAIN = {
        "flash_open": ["morning"],                     # 开盘闪电战 ← 当日盘前
        "morning": ["close", "night"],                 # 盘前简报 ← 上日收盘+夜报
        "midday_flash": ["morning", "flash_open"],     # P1-2: 盘中快照 ← 盘前+闪电
        "midday": ["morning", "flash_open", "midday_flash"], # 午间总结 ← 盘前+闪电+快照
        "afternoon": ["morning", "flash_open", "midday_flash", "midday"], # 下午速报 ← 今日全部
        "close": ["morning", "flash_open", "midday_flash", "midday", "afternoon"], # 收盘 ← 今日全部
        "night": ["close"],                            # 夜报 ← 当日收盘
    }

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._ensure_table()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_table(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cron_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    date TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    summary TEXT DEFAULT '',
                    key_metrics TEXT DEFAULT '{}',
                    prev_report_id INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cron_date ON cron_reports(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cron_type ON cron_reports(report_type)")

    @staticmethod
    def _is_placeholder_content(content: str) -> bool:
        if not content or not str(content).strip():
            return True
        s = str(content).strip()
        # 极短正文多为占位；阈值勿过大，以免误伤合法短摘要
        if len(s) < 30:
            return True
        low = s.lower()
        for m in CronReport._PLACEHOLDER_MARKERS:
            if m in s or m.lower() in low:
                return True
        return False

    @staticmethod
    def _read_artifact_json(path: str, max_chars: int = 450_000) -> str | None:
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read(max_chars + 1)
            truncated = len(raw) > max_chars
            if truncated:
                raw = raw[:max_chars] + "\n…[truncated for DB size cap]…\n"
            return raw.strip() or None
        except OSError:
            return None

    def _hydrate_content(self, report_type: str, content: str) -> tuple[str, dict]:
        """若正文为占位/过短，则注入 apps 产出的 JSON 全文（H6）。"""
        if report_type not in self.ARTIFACT_JSON_BY_REPORT_TYPE:
            return content, {}
        if not self._is_placeholder_content(content):
            return content, {}
        path = self.ARTIFACT_JSON_BY_REPORT_TYPE[report_type]
        blob = self._read_artifact_json(path)
        if not blob:
            return content, {"_hydrate_failed": path}
        header = (
            f"[AUTO_HYDRATED v1 report_type={report_type}]\n"
            f"原始 content 为过短/占位，已自 {path} 注入结构化快照供审计。\n"
            f"--- JSON begin ---\n"
        )
        footer = "\n--- JSON end ---\n"
        return header + blob + footer, {"_hydrated_from": path, "_hydrate": True}

    def save(self, job_name: str, content: str, summary: str = "", key_metrics: dict = None) -> int:
        """保存一份报告，返回ID"""
        now = datetime.now()
        report_type = self.REPORT_TYPES.get(job_name, "unknown")

        km = dict(key_metrics or {})
        content, extra = self._hydrate_content(report_type, content)
        km.update(extra)

        # 找到前置报告（同一交易日链）
        prev_id = None
        if report_type in self.CONTEXT_CHAIN:
            prev_types = self.CONTEXT_CHAIN[report_type]
            with self._conn() as conn:
                placeholders = ",".join("?" * len(prev_types))
                row = conn.execute(
                    f"SELECT id FROM cron_reports WHERE date=? AND report_type IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                    [now.strftime("%Y-%m-%d")] + prev_types
                ).fetchone()
                if row:
                    prev_id = row["id"]
            # 盘前简报特殊处理：查昨天的close/night
            if not prev_id and report_type == "morning":
                yesterday = (now - __import__('datetime').timedelta(days=1)).strftime("%Y-%m-%d")
                with self._conn() as conn:
                    row = conn.execute(
                        "SELECT id FROM cron_reports WHERE date=? AND report_type IN ('close','night') ORDER BY id DESC LIMIT 1",
                        [yesterday]
                    ).fetchone()
                    if row:
                        prev_id = row["id"]

        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO cron_reports (created_at, date, job_name, report_type, content, summary, key_metrics, prev_report_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now.isoformat(), now.strftime("%Y-%m-%d"),
                    job_name, report_type, content, summary,
                    json.dumps(km, ensure_ascii=False),
                    prev_id
                )
            )
            return cur.lastrowid

    def get_context(self, job_name: str, max_chars: int = 3000) -> str:
        """获取指定cron任务需要的上下文（从DB读前置报告）"""
        report_type = self.REPORT_TYPES.get(job_name, "")
        if report_type not in self.CONTEXT_CHAIN:
            return ""

        prev_types = self.CONTEXT_CHAIN[report_type]
        today = datetime.now().strftime("%Y-%m-%d")

        with self._conn() as conn:
            placeholders = ",".join("?" * len(prev_types))
            rows = conn.execute(
                f"SELECT job_name, summary, key_metrics FROM cron_reports WHERE date=? AND report_type IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                [today] + prev_types + [len(prev_types)]
            ).fetchall()

            # 盘前简报额外查昨天
            if not rows and report_type == "morning":
                yesterday = (datetime.now() - __import__('datetime').timedelta(days=1)).strftime("%Y-%m-%d")
                rows = conn.execute(
                    "SELECT job_name, summary, key_metrics FROM cron_reports WHERE date=? AND report_type IN ('close','night') ORDER BY id DESC LIMIT 2",
                    [yesterday]
                ).fetchall()

        if not rows:
            return ""

        parts = []
        for r in rows:
            parts.append(f"[{r['job_name']}] {r['summary']}")
            try:
                metrics = json.loads(r["key_metrics"])
                if metrics:
                    parts.append(json.dumps(metrics, ensure_ascii=False))
            except:
                pass

        context = "\n".join(parts)
        if len(context) > max_chars:
            context = context[:max_chars] + "..."
        return context

    def get_latest(self, report_type: str) -> dict:
        """获取最新一份指定类型的报告"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cron_reports WHERE report_type=? ORDER BY id DESC LIMIT 1",
                [report_type]
            ).fetchone()
        if not row:
            return {}
        d = dict(row)
        try: d["key_metrics"] = json.loads(d["key_metrics"])
        except: d["key_metrics"] = {}
        return d

    def get_by_id(self, report_id: int) -> dict:
        """P1-3 修复: 按ID获取报告（供 --ref 使用）"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cron_reports WHERE id=?",
                [report_id]
            ).fetchone()
        if not row:
            return {}
        d = dict(row)
        try: d["key_metrics"] = json.loads(d["key_metrics"])
        except: d["key_metrics"] = {}
        return d

    def get_today(self) -> list:
        """获取今日所有报告"""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT job_name, report_type, summary, key_metrics FROM cron_reports WHERE date=? ORDER BY id",
                [today]
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_old(self, days: int = 90):
        """清理旧报告"""
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d")
        with self._conn() as conn:
            conn.execute("DELETE FROM cron_reports WHERE date < ?", [cutoff])


# ========== JSON 行情快照 ==========

class MarketSnapshot:
    """最新行情快照 — JSON 覆盖写，不累计"""

    _cache = None
    _cache_mtime = 0

    def __init__(self, path=SNAPSHOT_PATH):
        self.path = path

    def _read(self) -> dict:
        """读取当前快照"""
        current_mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else 0
        if current_mtime != MarketSnapshot._cache_mtime:
            if os.path.exists(self.path):
                try:
                    with open(self.path) as f:
                        MarketSnapshot._cache = json.load(f)
                except:
                    MarketSnapshot._cache = {}
            else:
                MarketSnapshot._cache = {}
            MarketSnapshot._cache_mtime = current_mtime
        return MarketSnapshot._cache or {}

    def _write(self, data: dict):
        """写入快照"""
        with open(self.path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        MarketSnapshot._cache = data
        MarketSnapshot._cache_mtime = os.path.getmtime(self.path)

    def update(self, code: str, quote: dict):
        """更新单只标的行情"""
        data = self._read()
        data["_meta"] = {
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "updated_date": datetime.now().strftime("%Y-%m-%d")
        }
        if "quotes" not in data:
            data["quotes"] = {}
        data["quotes"][code] = quote
        self._write(data)

    def update_batch(self, quotes: dict):
        """批量更新行情 — P2-5: 覆盖而非合并，防止停更标的残留"""
        data = self._read()
        data["_meta"] = {
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "updated_date": datetime.now().strftime("%Y-%m-%d")
        }
        data["quotes"] = quotes  # P2-5: 直接覆盖，不合并旧数据

    def get(self, code: str) -> dict:
        """获取单只标的行情"""
        data = self._read()
        return data.get("quotes", {}).get(code)

    def get_all(self) -> dict:
        """获取所有标的行情"""
        data = self._read()
        return data.get("quotes", {})

    def get_meta(self) -> dict:
        """获取快照元信息"""
        data = self._read()
        return data.get("_meta", {})


# ========== JSON 当日计划 ==========

class DailyPlan:
    """当日操盘计划 — 盘前写入，盘中引用"""

    def __init__(self, path=PLAN_PATH):
        self.path = path

    def save(self, plan: dict):
        """保存计划"""
        plan["_meta"] = {
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "date": datetime.now().strftime("%Y-%m-%d")
        }
        with open(self.path, "w") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

    def load(self) -> dict:
        """读取计划"""
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path) as f:
                return json.load(f)
        except:
            return {}


# ========== 便捷函数 ==========

def log_and_snapshot(log_type: str, code: str, name: str, message: str, quote: dict = None):
    """一行代码完成：写日志 + 更新快照"""
    db = TradeDB()
    snap = MarketSnapshot()

    db.log(log_type, code, name, message, detail=quote or {})

    if quote:
        snap.update(code, quote)
