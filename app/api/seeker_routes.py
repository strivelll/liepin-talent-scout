"""求职端接口。

和招聘端接口并列，挂在 /api/seeker 下。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..config import config
from ..seeker import engine
from ..seeker.profile import load_profile

router = APIRouter(prefix="/api/seeker")


@router.get("/status")
def status() -> dict[str, Any]:
    st = engine.get_state()
    st["mode"] = config.mode
    st["in_window"] = engine._in_window()
    st["quota_left"] = engine._quota_left()
    p = load_profile()
    st["profile"] = {
        "name": p.name, "years": p.years, "city": p.city,
        "skills": p.skills,
        "expected": f"{p.salary_min // 1000}-{p.salary_max // 1000}K"
                    if p.salary_min and p.salary_max else "—",
        "titles": p.target_titles,
        "resume_policy": p.resume.get("send_policy"),
    }
    return st


@router.post("/control/{action}")
def control(action: str) -> dict[str, Any]:
    fn = {"start": engine.start, "pause": engine.pause,
          "resume": engine.resume, "stop": engine.stop}.get(action)
    if fn is None:
        raise HTTPException(400, f"未知操作: {action}")
    return fn()


@router.post("/tick")
def tick() -> dict[str, Any]:
    return engine.tick(once=True)


@router.post("/run")
def run(rounds: int = Query(8, ge=1, le=30)) -> dict[str, Any]:
    """演示：清空重跑，返回逐轮过程。"""
    from ..browser.seeker_driver import get_mock_seeker

    if config.mode == "mock":
        get_mock_seeker().reset()
        engine.reset_driver()

    conn = db.get_conn()
    for t in ("seek_jobs", "seek_messages", "seek_stats"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('seek_jobs','seek_messages')")
    conn.commit()
    db.init_db()

    # 演示压缩节奏
    run_cfg = config.seeker.setdefault("run", {})
    saved = {k: run_cfg.get(k) for k in ("greet_interval", "reply_interval")}
    run_cfg.update({"greet_interval": [0, 0.01], "reply_interval": [0, 0.01]})

    steps: list[dict[str, Any]] = []
    try:
        engine.start()
        for i in range(1, rounds + 1):
            r = engine.tick(once=True)
            s = r.get("stats", {})
            steps.append({
                "n": i,
                "collected": r.get("collected", 0),
                "matched": r.get("matched", 0),
                "greeted": r.get("greeted", 0),
                "replied": r.get("replied", 0),
                "resumes": r.get("resumes", 0),
                "scanned": s.get("scanned", 0),
                "matched_total": s.get("matched", 0),
                "greeted_total": s.get("greeted", 0),
                "replied_total": s.get("replied", 0),
                "resumes_total": s.get("resumes", 0),
                "interviews": s.get("interviews", 0),
            })
    finally:
        for k, v in saved.items():
            if v is not None:
                run_cfg[k] = v

    jobs = db.list_seek_jobs(limit=100)
    return {
        "steps": steps,
        "jobs": [
            {
                "id": j["id"], "title": j["title"], "company": j["company"],
                "salary": j["salary"], "score": j["match_score"],
                "state": j["state"], "pitch": j.get("match_pitch"),
                "reason": (j.get("match_reason") or "")[:70],
                "greeting": j.get("greeting"),
            }
            for j in jobs
        ],
        "stats": db.seek_today_stats(),
        "funnel": db.seek_funnel(),
    }


@router.get("/jobs")
def jobs(state: str | None = None, limit: int = Query(200, ge=1, le=500)) -> list[dict[str, Any]]:
    rows = db.list_seek_jobs(state=state, limit=limit)
    for r in rows:
        r.pop("jd_text", None)
    return rows


@router.get("/jobs/{jid}")
def job_detail(jid: int) -> dict[str, Any]:
    row = db.get_seek_job(jid)
    if row is None:
        raise HTTPException(404, "职位不存在")
    row["messages"] = db.get_seek_messages(jid)
    return row


@router.get("/report")
def report() -> dict[str, Any]:
    s = db.seek_today_stats()
    scanned = int(s.get("scanned", 0))
    greeted = int(s.get("greeted", 0))
    return {
        "date": date.today().isoformat(),
        "summary": {
            "扫描职位": scanned,
            "匹配通过": int(s.get("matched", 0)),
            "已投递": greeted,
            "收到回复": int(s.get("replied", 0)),
            "简历已发": int(s.get("resumes", 0)),
            "进入面试": int(s.get("interviews", 0)),
            "被拒": int(s.get("rejected", 0)),
            "投递转化": f"{greeted / scanned * 100:.0f}%" if scanned else "—",
        },
        "funnel": db.seek_funnel(),
    }


@router.post("/reset")
def reset() -> dict[str, Any]:
    from ..browser.seeker_driver import get_mock_seeker

    if config.mode == "mock":
        get_mock_seeker().reset()
    engine.reset_driver()
    conn = db.get_conn()
    for t in ("seek_jobs", "seek_messages", "seek_stats"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    db.log_event("info", "seeker", "求职端数据已重置")
    return {"ok": True}


@router.post("/risk")
def inject_risk(signal: str = Query("captcha")) -> dict[str, Any]:
    from ..browser.seeker_driver import get_mock_seeker

    if config.mode != "mock":
        return {"ok": False, "reason": "仅 mock 模式支持注入"}
    get_mock_seeker().inject_risk(signal or None)
    return {"ok": True, "risk": signal or None}
