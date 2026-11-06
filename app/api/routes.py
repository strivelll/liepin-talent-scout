"""HTTP 接口层。

全部走 /api 前缀，管理台是唯一的调用方。
不做鉴权——服务只监听 127.0.0.1，本机访问；如需多人使用再加。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..config import config
from ..core import pipeline
from ..llm import llm

router = APIRouter(prefix="/api")


# ---------------- 状态与控制 ----------------

@router.get("/status")
def status() -> dict[str, Any]:
    """运行状态 + 今日统计 + 驱动信息。"""
    state = pipeline.get_state()
    state["mode"] = config.mode
    state["llm_provider"] = llm.provider_name
    state["llm_enabled"] = llm.available
    state["jobs"] = len(config.active_jobs())
    state["now"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["in_window"] = pipeline.in_run_window()
    return state


@router.post("/control/{action}")
def control(action: str) -> dict[str, Any]:
    actions = {
        "start": pipeline.start,
        "pause": pipeline.pause,
        "resume": pipeline.resume,
        "stop": pipeline.stop,
    }
    fn = actions.get(action)
    if fn is None:
        raise HTTPException(400, f"未知操作: {action}")
    return fn()


@router.post("/tick")
def run_tick() -> dict[str, Any]:
    """手动跑一轮（演示与调试用）。"""
    return pipeline.tick(once=True)


# ---------------- 岗位与配置 ----------------

@router.get("/jobs")
def jobs() -> list[dict[str, Any]]:
    out = []
    for job in config.active_jobs():
        out.append({
            "id": job.id,
            "name": job.name,
            "headcount": job.headcount,
            "city": job.city,
            "flow_steps": len((job.chat or {}).get("flow") or []),
            "scripts": list((job.greeting or {}).get("script_ids") or []),
            "daily_cap": (job.greeting or {}).get("max_per_day"),
        })
    return out


@router.post("/config/reload")
def reload_config() -> dict[str, Any]:
    """热加载配置：改完 yaml 不用重启服务。"""
    config.reload()
    llm.reload()
    pipeline.reset_driver()
    db.log_event("info", "system", "配置已热加载")
    return {"ok": True, "jobs": len(config.active_jobs()), "mode": config.mode,
            "llm": llm.provider_name}


# ---------------- 候选人 ----------------

@router.get("/candidates")
def candidates(
    job_id: str | None = None,
    state: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    rows = db.list_candidates(job_id=job_id, state=state, limit=limit)
    for r in rows:
        try:
            r["tags"] = json.loads(r.get("tags") or "[]")
        except Exception:
            r["tags"] = []
        r.pop("raw_card", None)
    return rows


@router.get("/candidates/{cid}")
def candidate_detail(cid: int) -> dict[str, Any]:
    row = db.get_candidate(cid)
    if row is None:
        raise HTTPException(404, "候选人不存在")
    try:
        row["tags"] = json.loads(row.get("tags") or "[]")
    except Exception:
        row["tags"] = []
    row["messages"] = db.get_messages(cid)
    return row


@router.get("/funnel")
def funnel(job_id: str | None = None) -> list[dict[str, Any]]:
    return db.funnel_summary(job_id)


# ---------------- 日志与统计 ----------------

@router.get("/events")
def events(limit: int = Query(200, ge=1, le=1000), category: str | None = None) -> list[dict[str, Any]]:
    return db.list_events(limit=limit, category=category)


@router.get("/stats")
def stats() -> dict[str, Any]:
    return db.today_stats()


@router.get("/report/daily")
def daily_report() -> dict[str, Any]:
    """日报：管理台首屏与每日推送共用这份数据。

    口径说明（这几个数字客户会天天看，必须说清楚）：
    - 触达 = 打过招呼的候选人人数
    - 回复人数 = 回复过的候选人人数（不是消息条数）
    - 回复率 = 回复人数 / 触达人数
    - 消息数 = 对方发来的消息总条数，单独展示
    """
    s = db.today_stats()
    touched = db.count_touched()
    repliers = db.count_replied()
    them_msgs = db.count_them_messages()
    qualified = int(s.get("qualified", 0))
    invited = int(s.get("invited", 0))

    return {
        "date": date.today().isoformat(),
        "summary": {
            "触达人数": touched,
            "回复人数": repliers,
            "回复率": f"{repliers / touched * 100:.0f}%" if touched else "—",
            "对方消息": them_msgs,
            "合格": qualified,
            "约面": invited,
            "待人工": int(s.get("handover", 0)),
            "模型调用": int(s.get("llm_calls", 0)),
            "模型成本": f"{float(s.get('llm_cost', 0)):.4f} 元",
        },
        "funnel": db.funnel_summary(),
        "highlights": _highlights(),
    }


def _highlights() -> list[dict[str, Any]]:
    """挑出今日值得看的候选人：已约面的、待人工的。

    合格后系统会立即发邀约，状态随即变成 invited，
    所以只看 qualified 会一直是空的——两个状态都要取。
    """
    out: list[dict[str, Any]] = []

    for state, label in (("invited", "已约面"), ("qualified", "合格")):
        for cand in db.list_candidates(state=state, limit=10):
            reason = cand.get("score_reason") or ""
            if cand.get("invite_slot"):
                reason = f"{reason}｜面试时段：{cand['invite_slot']}"
            out.append({
                "id": cand["id"], "name": cand.get("name"), "job_id": cand.get("job_id"),
                "score": cand.get("score"), "state": label, "reason": reason,
            })

    for cand in db.list_candidates(state="handed_over", limit=10):
        out.append({
            "id": cand["id"], "name": cand.get("name"), "job_id": cand.get("job_id"),
            "score": None, "state": "待人工",
            "reason": "对话涉及薪资/合同，需人工接手",
        })
    return out




# ---------------- 演示接口（给客户看的 demo 用） ----------------

DEMO_SNAPSHOT = None


@router.get("/demo/snapshot")
def demo_snapshot() -> dict[str, Any]:
    """真实环境实测快照。

    数据来自 actual probe 抓取结果，落盘成文件而不是现场重跑——
    演示最怕现场翻车：网络抖动、突然弹验证码、页面改版，任何一个都会让 demo 崩掉。
    所以把"已经验证过的证据"固化成快照，现场只做展示。
    """
    global DEMO_SNAPSHOT
    if DEMO_SNAPSHOT is None:
        path = Path(__file__).resolve().parent.parent.parent / "data" / "demo_snapshot.json"
        if path.exists():
            DEMO_SNAPSHOT = json.loads(path.read_text(encoding="utf-8"))
        else:
            DEMO_SNAPSHOT = {"error": "缺少 demo_snapshot.json，请先运行 scripts/build_demo_snapshot.py"}
    return DEMO_SNAPSHOT


@router.post("/demo/run")
def demo_run(rounds: int = Query(10, ge=1, le=30)) -> dict[str, Any]:
    """演示模式：清空 → 跑 N 轮 → 返回逐轮过程和最终结果。

    返回完整的过程数据而不是只给结果，是为了让前端能做成"逐帧播放"，
    客户看到的是流程在推进，而不是一个已经跑完的静态表格。
    """
    from ..browser import get_mock

    get_mock().reset()
    pipeline.reset_driver()
    conn = db.get_conn()
    for table in ("messages", "events", "candidates", "daily_stats"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('messages','events','candidates')")
    conn.commit()
    db.init_db()

    # 演示时压缩节奏，否则一轮要等几十秒
    pacing = config.settings.setdefault("pacing", {})
    saved = {k: pacing.get(k) for k in ("greet_interval_sec", "reply_interval_sec", "read_dwell_sec")}
    pacing.update({
        "greet_interval_sec": [0, 0.01],
        "reply_interval_sec": [0, 0.01],
        "read_dwell_sec": [0, 0.01],
    })

    steps: list[dict[str, Any]] = []
    try:
        pipeline.start()
        for i in range(1, rounds + 1):
            r = pipeline.tick(once=True)
            st = r.get("stats", {})
            steps.append({
                "n": i,
                "replied": r.get("replied", 0),
                "greeted": r.get("greeted", 0),
                "judged": r.get("judged", 0),
                "touched": st.get("greeted", 0),
                "replied_candidates": db.count_replied(),
                "qualified": st.get("qualified", 0),
                "invited": st.get("invited", 0),
                "handover": st.get("handover", 0),
            })
    finally:
        for k, v in saved.items():
            if v is not None:
                pacing[k] = v

    cands = db.list_candidates(limit=100)
    report = daily_report()
    return {
        "steps": steps,
        "report": report,
        "candidates": [
            {
                "id": c["id"], "name": c.get("name"), "job_id": c.get("job_id"),
                "state": c["state"], "score": c.get("score"),
                "reason": (c.get("score_reason") or "")[:60],
                "invite_slot": c.get("invite_slot"),
                "steps_done": c.get("chat_step"),
            }
            for c in cands
        ],
    }


@router.post("/demo/risk")
def demo_risk(signal: str = Query("captcha")) -> dict[str, Any]:
    """注入一个风控信号，演示熔断是否生效。

    这是 demo 里最能建立信任的一环：客户最担心"账号被搞坏"，
    让他亲眼看到"一有异常系统立刻停"，比任何承诺都管用。
    """
    from ..browser import get_mock

    get_mock().inject_risk(signal or None)
    before = pipeline.get_state().get("running")
    r = pipeline.tick(once=True)
    stopped = r.get("stopped")
    return {
        "injected": signal,
        "stopped": stopped,
        "running_before": before,
        "running_after": pipeline.get_state().get("running"),
        "message": f"注入 {signal} → 系统{'已停止' if stopped else '未响应'}",
        "events": db.list_events(limit=3, category="risk"),
    }


@router.get("/demo/cost")
def demo_cost(candidates_per_day: int = Query(200, ge=10, le=2000)) -> dict[str, Any]:
    """三级漏斗的成本对比。

    用真实配置和真实单价算，不是拍脑袋：
    - 平台查看完整简历约 1 元/份（保守估计）
    - 软筛每次模型调用约 0.002 元
    """
    funnel = config.funnel
    deep_ratio = 0.15          # 实测：第三级只走约 15%
    soft_threshold = int(funnel.get("llm_prescreen_threshold", 60))

    price_resume = 1.0
    price_llm = 0.002

    naive = candidates_per_day * price_resume
    funnel_cost = candidates_per_day * price_llm + int(candidates_per_day * deep_ratio) * price_resume

    return {
        "daily_candidates": candidates_per_day,
        "naive": {
            "desc": "每个候选人都点开完整简历",
            "resume_views": candidates_per_day,
            "cost": round(naive, 2),
        },
        "funnel": {
            "desc": "硬筛 → 模型软筛 → 只对高分者精读",
            "resume_views": int(candidates_per_day * deep_ratio),
            "soft_calls": candidates_per_day,
            "cost": round(funnel_cost, 2),
        },
        "saved": round(naive - funnel_cost, 2),
        "saved_pct": f"{(1 - funnel_cost / naive) * 100:.0f}%" if naive else "0%",
        "monthly_saved": round((naive - funnel_cost) * 22, 2),
        "note": "按每月 22 个工作日估算；平台简历点数为保守估价，实际以账号权益为准",
    }


# ---------------- Mock 专用（演示熔断用） ----------------

@router.get("/mock/state")
def mock_state() -> dict[str, Any]:
    from ..browser import get_mock

    m = get_mock()
    return {
        "greeted": len(m._greeted),
        "unread": sorted(m._unread),
        "risk": m._risk,
    }


@router.post("/mock/reset")
def mock_reset(clear_db: bool = Query(True)) -> dict[str, Any]:
    """重置 Mock 状态。

    默认同时清空数据库——演示时经常需要"从头来一遍"，
    如果只重置内存里的模拟会话而库里候选人还是已约面状态，
    再跑就不会有任何动作，容易误判成系统坏了。
    """
    from ..browser import get_mock

    get_mock().reset()
    pipeline.reset_driver()

    if clear_db:
        conn = db.get_conn()
        for table in ("messages", "events", "candidates", "daily_stats"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('messages','events','candidates')")
        conn.commit()

    db.log_event("info", "system", "Mock 状态已重置" + ("（含数据库）" if clear_db else ""))
    return {"ok": True, "db_cleared": clear_db}


@router.post("/mock/risk")
def mock_risk(signal: str = Query("captcha")) -> dict[str, Any]:
    """注入风控信号，用于演示熔断。signal 传空字符串表示清除。"""
    from ..browser import get_mock

    get_mock().inject_risk(signal or None)
    return {"ok": True, "risk": signal or None}
