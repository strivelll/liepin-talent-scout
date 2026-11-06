"""人才库：把采集到的简历变成可检索的库。

**为什么需要**
简历采集完之后是散在 `data/resumes/` 下的几十个文件夹，靠 ls 找不到人。
这个模块把每份简历抽成一行记录，支持按姓名/公司/技能/城市检索。

**与 db.py 的关系**
`db.py` 里的 `candidates` 表是给 BOSS 招聘端（岗位投递流）用的，字段是 job_id/state 那套。
猎聘这边的简历是主动寻访来的，字段完全不同（期望/技能/工作经历），所以单独建表，
不混在一起——混了以后迁移和查询都麻烦。

**数据来源**
不重复解析：直接读 `data/resumes/{姓名}_{时间}/` 里已有的 `resume.json` 和 `resume.txt`。
采集脚本负责落盘，这个模块负责索引。
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "recruit.db"
RESUME_DIR = ROOT / "data" / "resumes"

# 只有 {姓名}_{YYYYMMDD_HHMMSS} 才是采集脚本产出的简历目录。
# data/resumes/ 下还有 library_html / library_meta / library 等辅助目录，
# 里面也有 resume.txt，不过滤的话会被当成"人才"塞进库里。
RESUME_DIR_RE = re.compile(r"^.+_\d{8}_\d{6}$")


def is_resume_dir(name: str) -> bool:
    """判断目录名是否是采集产物（防止辅助目录污染人才库）。"""
    return bool(RESUME_DIR_RE.match(str(name or "")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS talent (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    age           TEXT,
    years         TEXT,
    edu           TEXT,
    city          TEXT,
    expect        TEXT,
    company       TEXT,
    skills        TEXT,          -- JSON 数组
    resume_text   TEXT,
    dir_path      TEXT UNIQUE,   -- 采集目录，作为去重键
    has_pdf       INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'liepin',
    score         INTEGER,       -- AI 匹配分（来自 scores.json，可能为空）
    status        TEXT DEFAULT 'new',  -- new / contacted / replied / archived
    note          TEXT,
    collected_at  TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_talent_name ON talent(name);
CREATE INDEX IF NOT EXISTS idx_talent_city ON talent(city);
CREATE INDEX IF NOT EXISTS idx_talent_status ON talent(status);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session():
    """用完即关的连接上下文。

    `with sqlite3.connect(...) as c` 只管事务、**不会关闭连接**，
    Windows 下库文件会一直被占用（采集脚本跑完临时库删不掉就是这么来的）。
    """
    c = connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init() -> None:
    with session() as c:
        c.executescript(SCHEMA)


# ---------------------------------------------------------------- 解析

def _parse_dir(d: Path) -> dict[str, Any] | None:
    """从一个采集目录里抽字段。抽不到的留空，不编。"""
    txt_file = d / "resume.txt"
    if not txt_file.exists():
        return None
    text = txt_file.read_text(encoding="utf-8", errors="replace")
    if len(text) < 40:
        return None

    # 目录名形如 蔡示例_20261104_102351
    name = d.name.rsplit("_", 2)[0]

    # 优先读采集时写下的 json
    meta: dict[str, Any] = {}
    j = d / "resume.json"
    if j.exists():
        try:
            meta = json.loads(j.read_text(encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            meta = {}

    lines = [x.strip() for x in text.split("\n") if x.strip()]

    def after(label: str, offset: int = 1) -> str:
        for i, ln in enumerate(lines):
            if ln == label and i + offset < len(lines):
                return lines[i + offset]
        return ""

    # 期望职位：紧跟「求职意向」那行
    expect = after("求职意向")
    city = ""
    m = re.search(r"·\s*([^\s·]+)", text)
    if m:
        city = m.group(1)

    # 公司：工作经历段第一行的公司名
    company = ""
    try:
        idx = lines.index("工作经历")
        if idx + 1 < len(lines):
            company = lines[idx + 1]
    except ValueError:
        pass

    edu = ""
    for ln in lines:
        if any(k in ln for k in ("本科", "硕士", "博士", "大专")):
            edu = ln.split("·")[-1].strip() if "·" in ln else ln
            edu = edu[:10]
            break

    years = ""
    m = re.search(r"工作\s*(\d+)\s*年", text)
    if m:
        years = m.group(1) + "年"

    # 技能：常见技术词
    skills = re.findall(
        r"\b(Java|Python|Go|C\+\+|Spring|Spring\s*Cloud|MySQL|Oracle|Redis|Kafka|"
        r"Hadoop|Spark|GIS|CIM|Android|Vue|React|Docker|Kubernetes|微服务|分布式|"
        r"架构设计|项目管理|团队管理)\b",
        text,
    )
    skills = list(dict.fromkeys(s.replace(" ", "") for s in skills))[:12]

    # 采集时间
    collected = ""
    m = re.match(r".*_(\d{8})_(\d{6})$", d.name)
    if m:
        collected = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} {m.group(2)[:2]}:{m.group(2)[2:4]}"

    return {
        "name": name,
        "age": str(meta.get("_年龄") or ""),
        "years": years,
        "edu": edu,
        "city": city,
        "expect": expect,
        "company": company,
        "skills": skills,
        "resume_text": text,
        "dir_path": str(d),
        "has_pdf": 1 if (d / "resume.pdf").exists() else 0,
        "note": str(meta.get("_联系方式") or ""),
        "_联系方式已回复": meta.get("_联系方式已回复"),
        "collected_at": collected or _now(),
    }


def import_dirs(only_replied: bool = True) -> dict[str, int]:
    """扫 resumes 目录，把新的简历入库。已存在的按 dir_path 跳过。

    only_replied=True（默认，客户要求）：只入库「对方已回复联系方式」的简历。
    采集脚本会把 `_联系方式已回复` 写进 resume.json；没有这个标记的是
    旧版未筛选的历史产物，不进人才库。
    """
    init()
    stats = {"新增": 0, "已存在": 0, "跳过": 0}
    if not RESUME_DIR.exists():
        return stats

    # 顺带把 AI 评分也带进来（按姓名匹配）
    scores: dict[str, int] = {}
    sf = ROOT / "data" / "liepin-probe" / "scores.json"
    if sf.exists():
        try:
            raw = json.loads(sf.read_text(encoding="utf-8"))
            for k, v in raw.items():
                nm = k.split("|")[0]
                if isinstance(v, dict) and isinstance(v.get("score"), int):
                    scores[nm] = max(scores.get(nm, 0), v["score"])
        except Exception:                         # noqa: BLE001
            pass

    with session() as c:
        for d in sorted(RESUME_DIR.iterdir()):
            if not d.is_dir():
                continue
            if not is_resume_dir(d.name):
                stats["跳过"] += 1
                continue
            exists = c.execute("SELECT 1 FROM talent WHERE dir_path = ?", (str(d),)).fetchone()
            if exists:
                stats["已存在"] += 1
                continue
            row = _parse_dir(d)
            if row is None:
                stats["跳过"] += 1
                continue
            replied = row.pop("_联系方式已回复", None)
            if only_replied and replied is not True:
                stats["跳过"] += 1
                continue
            row["score"] = scores.get(row["name"])
            row["updated_at"] = _now()
            cols = ", ".join(row.keys())
            ph = ", ".join("?" * len(row))
            c.execute(f"INSERT INTO talent ({cols}) VALUES ({ph})",
                      tuple(json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
                            for v in row.values()))
            stats["新增"] += 1
    return stats


# ---------------------------------------------------------------- 查询

def list_talent(keyword: str = "", status: str = "", limit: int = 200) -> list[dict[str, Any]]:
    init()
    sql = "SELECT id,name,age,years,edu,city,expect,company,skills,has_pdf,score,status,note,collected_at FROM talent"
    where, args = [], []
    if keyword:
        where.append("(name LIKE ? OR company LIKE ? OR city LIKE ? OR expect LIKE ? OR skills LIKE ? OR resume_text LIKE ?)")
        like = f"%{keyword}%"
        args += [like] * 6
    if status:
        where.append("status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(score, -1) DESC, collected_at DESC LIMIT ?"
    args.append(limit)

    with session() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["skills"] = json.loads(d.get("skills") or "[]")
        except Exception:                         # noqa: BLE001
            d["skills"] = []
        d["resume_text"] = ""                     # 列表不传全文，详情接口再给
        out.append(d)
    return out


def get_talent(tid: int) -> dict[str, Any] | None:
    init()
    with session() as c:
        r = c.execute("SELECT * FROM talent WHERE id = ?", (tid,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    try:
        d["skills"] = json.loads(d.get("skills") or "[]")
    except Exception:                             # noqa: BLE001
        d["skills"] = []
    return d


def update_talent(tid: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with session() as c:
        c.execute(f"UPDATE talent SET {cols} WHERE id = ?",
                  (*fields.values(), tid))


def stats() -> dict[str, Any]:
    init()
    with session() as c:
        total = c.execute("SELECT COUNT(*) FROM talent").fetchone()[0]
        with_pdf = c.execute("SELECT COUNT(*) FROM talent WHERE has_pdf=1").fetchone()[0]
        scored = c.execute("SELECT COUNT(*) FROM talent WHERE score IS NOT NULL").fetchone()[0]
        by_status = {r[0]: r[1] for r in c.execute(
            "SELECT status, COUNT(*) FROM talent GROUP BY status").fetchall()}
    return {"total": total, "with_pdf": with_pdf, "scored": scored, "by_status": by_status}


if __name__ == "__main__":
    print("导入人才库…")
    print(import_dirs())
    print(stats())
