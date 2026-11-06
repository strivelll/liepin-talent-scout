"""SQLite 数据层。

只做三件事：建表、读写、统计。
不引入 ORM，避免依赖膨胀；SQL 全部参数绑定，防注入。

并发说明：调度线程与 API 线程共用同一连接，用可重入锁串行化。
MVP 阶段量级很小（日均几十到几百条），不需要连接池。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from typing import Any, Iterable

from .config import config

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    ext_id          TEXT NOT NULL,
    name            TEXT,
    age             INTEGER,
    education       TEXT,
    experience      TEXT,
    salary_expect   TEXT,
    active_status   TEXT,
    company         TEXT,
    tags            TEXT,
    raw_card        TEXT,
    score           INTEGER,
    score_reason    TEXT,
    state           TEXT NOT NULL DEFAULT 'pending',
    chat_step       INTEGER NOT NULL DEFAULT 0,
    followup_count  INTEGER NOT NULL DEFAULT 0,
    greeting_text   TEXT,
    invite_slot     TEXT,
    invite_sent_at  TEXT,
    reject_reason   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(job_id, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_cand_state ON candidates(state);
CREATE INDEX IF NOT EXISTS idx_cand_job ON candidates(job_id);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    role         TEXT NOT NULL,          -- them / us / system
    content      TEXT NOT NULL,
    intent       TEXT,                   -- 判定结果：qualified / rejected / need_followup / handover
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_cand ON messages(candidate_id, id);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    level        TEXT NOT NULL,          -- info / warn / error
    category     TEXT NOT NULL,          -- greet / chat / judge / invite / risk / system
    message      TEXT NOT NULL,
    candidate_id INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evt_time ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_evt_cat ON events(category);

CREATE TABLE IF NOT EXISTS daily_stats (
    day         TEXT PRIMARY KEY,
    greeted     INTEGER NOT NULL DEFAULT 0,
    replied     INTEGER NOT NULL DEFAULT 0,
    qualified   INTEGER NOT NULL DEFAULT 0,
    rejected    INTEGER NOT NULL DEFAULT 0,
    invited     INTEGER NOT NULL DEFAULT 0,
    handover    INTEGER NOT NULL DEFAULT 0,
    llm_calls   INTEGER NOT NULL DEFAULT 0,
    llm_tokens  INTEGER NOT NULL DEFAULT 0,
    llm_cost    REAL NOT NULL DEFAULT 0
);

-- ===== 求职端（C 端）=====

CREATE TABLE IF NOT EXISTS seek_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ext_id       TEXT NOT NULL UNIQUE,
    title        TEXT,
    company      TEXT,
    salary       TEXT,
    salary_min   INTEGER,
    salary_max   INTEGER,
    city         TEXT,
    district     TEXT,
    tags         TEXT,
    url          TEXT,
    jd_text      TEXT,
    match_score  INTEGER,
    match_reason TEXT,
    match_pitch  TEXT,          -- 打招呼用的"钩子"，由模型生成
    state        TEXT NOT NULL DEFAULT 'new',
    greeting     TEXT,
    resume_sent  INTEGER NOT NULL DEFAULT 0,
    reject_why   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seek_state ON seek_jobs(state);
CREATE INDEX IF NOT EXISTS idx_seek_score ON seek_jobs(match_score);

CREATE TABLE IF NOT EXISTS seek_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    role       TEXT NOT NULL,     -- them=HR / me=我 / system=系统提示
    content    TEXT NOT NULL,
    intent     TEXT,              -- ask_salary / ask_exp / want_resume / invite / reject / other
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seek_msg_job ON seek_messages(job_id, id);

CREATE TABLE IF NOT EXISTS seek_stats (
    day       TEXT PRIMARY KEY,
    scanned   INTEGER NOT NULL DEFAULT 0,
    matched   INTEGER NOT NULL DEFAULT 0,
    greeted   INTEGER NOT NULL DEFAULT 0,
    replied   INTEGER NOT NULL DEFAULT 0,
    resumes   INTEGER NOT NULL DEFAULT 0,
    interviews INTEGER NOT NULL DEFAULT 0,
    rejected  INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn() -> sqlite3.Connection:
    """获取全局连接（首次调用时初始化）。"""
    global _CONN
    with _LOCK:
        if _CONN is None:
            db_path = config.path("db_path", "data/recruit.db")
            _CONN = sqlite3.connect(str(db_path), check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.execute("PRAGMA synchronous=NORMAL")
        return _CONN


def init_db() -> None:
    """建表（幂等）。"""
    with _LOCK:
        get_conn().executescript(SCHEMA)
        get_conn().commit()


# ---------------- 候选人 ----------------

def upsert_candidate(job_id: str, card: dict[str, Any]) -> int:
    """按 (job_id, ext_id) 幂等写入候选人，返回主键。

    已存在时不覆盖其 state / chat_step 等进度字段，只补充基础信息。
    """
    ext_id = str(card.get("ext_id") or "")
    if not ext_id:
        raise ValueError("candidate.ext_id 不能为空")
    now = _now()
    with _LOCK:
        conn = get_conn()
        row = conn.execute(
            "SELECT id FROM candidates WHERE job_id=? AND ext_id=?", (job_id, ext_id)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE candidates SET name=?, age=?, education=?, experience=?,
                       salary_expect=?, active_status=?, company=?, tags=?,
                       raw_card=?, updated_at=? WHERE id=?""",
                (
                    card.get("name"), card.get("age"), card.get("education"),
                    card.get("experience"), card.get("salary_expect"),
                    card.get("active_status"), card.get("company"),
                    json.dumps(card.get("tags") or [], ensure_ascii=False),
                    json.dumps(card, ensure_ascii=False), now, row["id"],
                ),
            )
            conn.commit()
            return int(row["id"])

        cur = conn.execute(
            """INSERT INTO candidates
               (job_id, ext_id, name, age, education, experience, salary_expect,
                active_status, company, tags, raw_card, state, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)""",
            (
                job_id, ext_id, card.get("name"), card.get("age"), card.get("education"),
                card.get("experience"), card.get("salary_expect"), card.get("active_status"),
                card.get("company"), json.dumps(card.get("tags") or [], ensure_ascii=False),
                json.dumps(card, ensure_ascii=False), now, now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_candidate(cid: int, **fields: Any) -> None:
    """更新候选人字段（白名单校验，避免拼出非法列名）。"""
    allowed = {
        "state", "chat_step", "followup_count", "score", "score_reason",
        "greeting_text", "invite_slot", "invite_sent_at", "reject_reason",
    }
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"不允许更新字段: {k}")
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(cid)
    with _LOCK:
        conn = get_conn()
        conn.execute(f"UPDATE candidates SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()


def get_candidate(cid: int) -> dict[str, Any] | None:
    with _LOCK:
        row = get_conn().execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None


def list_candidates(
    job_id: str | None = None,
    state: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """按条件查询候选人，按更新时间倒序。"""
    sql = "SELECT * FROM candidates WHERE 1=1"
    args: list[Any] = []
    if job_id:
        sql += " AND job_id=?"
        args.append(job_id)
    if state:
        sql += " AND state=?"
        args.append(state)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    with _LOCK:
        rows = get_conn().execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def candidates_by_state(states: Iterable[str], job_id: str | None = None) -> list[dict[str, Any]]:
    """按状态集合批量取候选人（调度器用）。"""
    states = list(states)
    if not states:
        return []
    marks = ",".join("?" * len(states))
    sql = f"SELECT * FROM candidates WHERE state IN ({marks})"
    args: list[Any] = states
    if job_id:
        sql += " AND job_id=?"
        args.append(job_id)
    sql += " ORDER BY updated_at ASC"
    with _LOCK:
        rows = get_conn().execute(sql, args).fetchall()
        return [dict(r) for r in rows]


# ---------------- 消息 ----------------

def add_message(candidate_id: int, role: str, content: str, intent: str | None = None) -> int:
    with _LOCK:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO messages (candidate_id, role, content, intent, created_at) VALUES (?,?,?,?,?)",
            (candidate_id, role, content, intent, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_messages(candidate_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with _LOCK:
        rows = get_conn().execute(
            "SELECT * FROM messages WHERE candidate_id=? ORDER BY id ASC LIMIT ?",
            (candidate_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------- 事件日志 ----------------

def log_event(level: str, category: str, message: str, candidate_id: int | None = None) -> None:
    with _LOCK:
        conn = get_conn()
        conn.execute(
            "INSERT INTO events (level, category, message, candidate_id, created_at) VALUES (?,?,?,?,?)",
            (level, category, message, candidate_id, _now()),
        )
        conn.commit()


def list_events(limit: int = 200, category: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM events"
    args: list[Any] = []
    if category:
        sql += " WHERE category=?"
        args.append(category)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _LOCK:
        rows = get_conn().execute(sql, args).fetchall()
        return [dict(r) for r in rows]


# ---------------- 统计 ----------------

def bump_stat(field: str, n: int = 1) -> None:
    """累加当日统计（字段名白名单校验）。"""
    allowed = {"greeted", "replied", "qualified", "rejected", "invited", "handover", "llm_calls"}
    if field not in allowed:
        raise ValueError(f"非法统计字段: {field}")
    day = date.today().isoformat()
    with _LOCK:
        conn = get_conn()
        conn.execute("INSERT OR IGNORE INTO daily_stats (day) VALUES (?)", (day,))
        conn.execute(f"UPDATE daily_stats SET {field}={field}+? WHERE day=?", (n, day))
        conn.commit()


def add_llm_cost(tokens: int, cost: float) -> None:
    day = date.today().isoformat()
    with _LOCK:
        conn = get_conn()
        conn.execute("INSERT OR IGNORE INTO daily_stats (day) VALUES (?)", (day,))
        conn.execute(
            "UPDATE daily_stats SET llm_calls=llm_calls+1, llm_tokens=llm_tokens+?, llm_cost=llm_cost+? WHERE day=?",
            (tokens, cost, day),
        )
        conn.commit()


def today_stats() -> dict[str, Any]:
    day = date.today().isoformat()
    with _LOCK:
        row = get_conn().execute("SELECT * FROM daily_stats WHERE day=?", (day,)).fetchone()
        if not row:
            return {
                "day": day, "greeted": 0, "replied": 0, "qualified": 0, "rejected": 0,
                "invited": 0, "handover": 0, "llm_calls": 0, "llm_tokens": 0, "llm_cost": 0.0,
            }
        return dict(row)


def count_today_greeted() -> int:
    """今日已打招呼数。

    以 events 表里成功的 greet 事件为事实源，而不是"状态为已触达的候选人数"——
    后者会把昨天打招呼、今天才回复的人算进今天，导致日上限判断偏早触发。
    """
    today = date.today().isoformat()
    with _LOCK:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM events WHERE category='greet' AND level='info' AND substr(created_at,1,10)=?",
            (today,),
        ).fetchone()
        return int(row["n"]) if row else 0


def count_touched() -> int:
    """被触达过的候选人总数（打过招呼的）。"""
    with _LOCK:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM candidates "
            "WHERE state IN ('greeted','chatting','qualified','rejected','invited','handed_over')"
        ).fetchone()
        return int(row["n"]) if row else 0


def count_replied() -> int:
    """有过回复的候选人**人数**（不是消息条数）。

    这是回复率的分子。用消息条数当分子会算出 400% 这种荒谬数字——
    一个人聊 5 轮是 5 条消息，但只算 1 个回复的人。
    """
    with _LOCK:
        row = get_conn().execute(
            "SELECT COUNT(DISTINCT candidate_id) AS n FROM messages WHERE role='them'"
        ).fetchone()
        return int(row["n"]) if row else 0


def count_them_messages() -> int:
    """对方消息总条数，单独展示，不参与比率计算。"""
    with _LOCK:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM messages WHERE role='them'"
        ).fetchone()
        return int(row["n"]) if row else 0


# ============ 求职端 DAO ============

def upsert_seek_job(job: dict[str, Any]) -> int:
    """写入/更新职位。按 ext_id 幂等，已有进度字段不覆盖。"""
    ext_id = str(job.get("ext_id") or "")
    if not ext_id:
        raise ValueError("seek_job.ext_id 不能为空")
    now = _now()
    tags = job.get("tags") or []
    if isinstance(tags, list):
        tags = json.dumps(tags, ensure_ascii=False)

    with _LOCK:
        conn = get_conn()
        row = conn.execute("SELECT id FROM seek_jobs WHERE ext_id=?", (ext_id,)).fetchone()
        if row:
            conn.execute(
                """UPDATE seek_jobs SET title=?, company=?, salary=?, salary_min=?, salary_max=?,
                       city=?, district=?, tags=?, url=?, jd_text=?, updated_at=? WHERE id=?""",
                (job.get("title"), job.get("company"), job.get("salary"),
                 job.get("salary_min"), job.get("salary_max"), job.get("city"),
                 job.get("district"), tags, job.get("url"), job.get("jd_text"),
                 now, row["id"]),
            )
            conn.commit()
            return int(row["id"])

        cur = conn.execute(
            """INSERT INTO seek_jobs
               (ext_id, title, company, salary, salary_min, salary_max, city, district,
                tags, url, jd_text, state, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'new', ?, ?)""",
            (ext_id, job.get("title"), job.get("company"), job.get("salary"),
             job.get("salary_min"), job.get("salary_max"), job.get("city"),
             job.get("district"), tags, job.get("url"), job.get("jd_text"), now, now),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_seek_job(jid: int, **fields: Any) -> None:
    allowed = {
        "state", "match_score", "match_reason", "match_pitch",
        "greeting", "resume_sent", "reject_why", "jd_text",
    }
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"不允许更新字段: {k}")
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(jid)
    with _LOCK:
        conn = get_conn()
        conn.execute(f"UPDATE seek_jobs SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()


def get_seek_job(jid: int) -> dict[str, Any] | None:
    with _LOCK:
        row = get_conn().execute("SELECT * FROM seek_jobs WHERE id=?", (jid,)).fetchone()
        return dict(row) if row else None


def get_seek_job_by_ext(ext_id: str) -> dict[str, Any] | None:
    with _LOCK:
        row = get_conn().execute("SELECT * FROM seek_jobs WHERE ext_id=?", (ext_id,)).fetchone()
        return dict(row) if row else None


def list_seek_jobs(state: str | None = None, limit: int = 200,
                   min_score: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM seek_jobs WHERE 1=1"
    args: list[Any] = []
    if state:
        sql += " AND state=?"
        args.append(state)
    if min_score is not None:
        sql += " AND match_score >= ?"
        args.append(min_score)
    sql += " ORDER BY match_score DESC, updated_at DESC LIMIT ?"
    args.append(limit)
    with _LOCK:
        rows = get_conn().execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except Exception:
                d["tags"] = []
            out.append(d)
        return out


def seek_jobs_by_state(states: list[str], limit: int = 100) -> list[dict[str, Any]]:
    if not states:
        return []
    marks = ",".join("?" * len(states))
    with _LOCK:
        rows = get_conn().execute(
            f"SELECT * FROM seek_jobs WHERE state IN ({marks}) ORDER BY match_score DESC LIMIT ?",
            (*states, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def add_seek_message(job_id: int, role: str, content: str, intent: str | None = None) -> int:
    with _LOCK:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO seek_messages (job_id, role, content, intent, created_at) VALUES (?,?,?,?,?)",
            (job_id, role, content, intent, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_seek_messages(job_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with _LOCK:
        rows = get_conn().execute(
            "SELECT * FROM seek_messages WHERE job_id=? ORDER BY id ASC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def bump_seek_stat(field: str, n: int = 1) -> None:
    allowed = {"scanned", "matched", "greeted", "replied", "resumes", "interviews", "rejected"}
    if field not in allowed:
        raise ValueError(f"非法统计字段: {field}")
    day = date.today().isoformat()
    with _LOCK:
        conn = get_conn()
        conn.execute("INSERT OR IGNORE INTO seek_stats (day) VALUES (?)", (day,))
        conn.execute(f"UPDATE seek_stats SET {field}={field}+? WHERE day=?", (n, day))
        conn.commit()


def seek_today_stats() -> dict[str, Any]:
    day = date.today().isoformat()
    with _LOCK:
        row = get_conn().execute("SELECT * FROM seek_stats WHERE day=?", (day,)).fetchone()
        if not row:
            return {"day": day, "scanned": 0, "matched": 0, "greeted": 0,
                    "replied": 0, "resumes": 0, "interviews": 0, "rejected": 0}
        return dict(row)


def count_seek_greeted_today() -> int:
    """今日已打招呼数，以 seek_stats 为准（不依赖职位状态，避免跨天串味）。"""
    return int(seek_today_stats().get("greeted", 0))


def seek_funnel() -> list[dict[str, Any]]:
    with _LOCK:
        rows = get_conn().execute(
            "SELECT state, COUNT(*) AS n FROM seek_jobs GROUP BY state"
        ).fetchall()
        return [dict(r) for r in rows]


def funnel_summary(job_id: str | None = None) -> list[dict[str, Any]]:
    """漏斗分布，供管理台展示。"""
    sql = "SELECT state, COUNT(*) AS n FROM candidates"
    args: list[Any] = []
    if job_id:
        sql += " WHERE job_id=?"
        args.append(job_id)
    sql += " GROUP BY state"
    with _LOCK:
        rows = get_conn().execute(sql, args).fetchall()
        return [dict(r) for r in rows]
