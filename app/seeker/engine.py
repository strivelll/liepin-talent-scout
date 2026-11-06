"""求职端主流程编排。

一次 tick 的顺序，和招聘端逻辑一致但方向相反：

1. **采集**：读职位列表入库
2. **匹配**：硬筛 + 模型打分，过线的标 scored，没过的标 skipped
3. **打招呼**：对 scored 的职位发起沟通（受日限额约束）
4. **回消息**：处理 HR 的未读，按意图回复
5. **发简历**：HR 要简历、或聊够轮次时发送

顺序上「回消息」优先于「打招呼」：已经在聊的机会比新机会值钱得多，
而且会话窗口有限，攒着不回会凉。
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from typing import Any

from ..browser.seeker_driver import SeekerDriver, get_mock_seeker, get_seeker_driver
from ..config import config
from ..db import (
    bump_seek_stat,
    count_seek_greeted_today,
    get_seek_job,
    get_seek_job_by_ext,
    get_seek_messages,
    list_seek_jobs,
    log_event,
    seek_funnel,
    seek_jobs_by_state,
    seek_today_stats,
    update_seek_job,
    upsert_seek_job,
)
from . import dialog as dialog_mod
from . import scripts as script_gen
from .matcher import JobInfo, run_match
from .profile import load_profile

_LOCK = threading.RLock()
_DRIVER: SeekerDriver | None = None
_DRIVER_LOCK = threading.Lock()

_state: dict[str, Any] = {
    "running": False,
    "paused": False,
    "last_tick": None,
    "last_error": None,
    "stop_reason": None,
    "tick_count": 0,
}


def get_state() -> dict[str, Any]:
    with _LOCK:
        snap = dict(_state)
    snap["stats"] = seek_today_stats()
    snap["greeted_today"] = count_seek_greeted_today()
    snap["funnel"] = seek_funnel()
    return snap


def _driver() -> SeekerDriver:
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is None:
            _DRIVER = get_seeker_driver()
            _DRIVER.open()
        return _DRIVER


def reset_driver() -> None:
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is not None:
            try:
                _DRIVER.close()
            except Exception:
                pass
        _DRIVER = None


def _cfg() -> dict[str, Any]:
    return (config.seeker or {}).get("run", {}) or {}


def _sleep(key: str) -> None:
    """随机等待。mock 模式压到毫秒级，否则演示等不起。"""
    if config.mode != "real":
        time.sleep(random.uniform(0.02, 0.1))
        return
    lo, hi = _cfg().get(key, [5, 15])
    time.sleep(random.uniform(float(lo), float(hi)))


def _in_window() -> bool:
    window = _cfg().get("window") or ["00:00", "23:59"]
    try:
        start = datetime.strptime(str(window[0]), "%H:%M").time()
        end = datetime.strptime(str(window[1]), "%H:%M").time()
    except Exception:
        return True
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def _quota_left() -> int:
    cap = int(_cfg().get("daily_greet_limit", 30) or 30)
    return max(0, cap - count_seek_greeted_today())


# ---------------- 各环节 ----------------

def step_collect(driver: SeekerDriver, limit: int = 15) -> int:
    """采集职位入库。已存在的只更新基础字段，不动进度。"""
    jobs = driver.fetch_jobs(limit=limit)
    added = 0
    for job in jobs:
        existing = get_seek_job_by_ext(job.ext_id)
        jid = upsert_seek_job(job.to_row())
        if existing is None:
            added += 1
        bump_seek_stat("scanned") if existing is None else None
    if added:
        log_event("info", "seeker", f"采集到 {added} 个新职位")
    return added


def step_match(limit: int = 12) -> int:
    """对未处理的职位跑匹配。"""
    profile = load_profile()
    pending = seek_jobs_by_state(["new"], limit=limit)
    done = 0
    for row in pending:
        job = _row_to_job(row)
        m = run_match(job, profile)
        update_seek_job(int(row["id"]), **m.to_row())
        if m.passed:
            update_seek_job(int(row["id"]), state="scored")
            bump_seek_stat("matched")
        else:
            update_seek_job(int(row["id"]), state="skipped", reject_why=m.reason)
        log_event(
            "info", "seeker",
            f"「{job.title}」匹配 {m.score} 分（{m.stage}）：{m.reason[:50]}",
            None,
        )
        done += 1
    return done


def step_greet(driver: SeekerDriver, limit: int = 5) -> int:
    """对过线且未投的职位打招呼。"""
    profile = load_profile()
    quota = _quota_left()
    if quota <= 0:
        return 0

    rows = seek_jobs_by_state(["scored"], limit=max(limit * 3, 12))
    greeted = 0
    for row in rows:
        if greeted >= limit or greeted >= quota:
            break
        if driver.risk_signal():
            break

        job = _row_to_job(row)
        from .matcher import MatchResult

        m = MatchResult(
            passed=True, stage="stored",
            score=int(row.get("match_score") or 0),
            reason=str(row.get("match_reason") or ""),
            pitch=str(row.get("match_pitch") or ""),
        )
        text = script_gen.greeting(job, m, profile, seed=int(row["id"]))
        _sleep("greet_interval")

        if driver.greet(job, text):
            update_seek_job(int(row["id"]), state="greeted", greeting=text)
            from ..db import add_seek_message

            jid = int(row["id"])
            if not get_seek_messages(jid, limit=1):
                add_seek_message(jid, "me", text)
            bump_seek_stat("greeted")
            greeted += 1
            log_event("info", "seeker", f"已向「{job.title}·{job.company}」打招呼", None)
        else:
            update_seek_job(int(row["id"]), state="greet_failed")
            log_event("warn", "seeker", f"「{job.title}」打招呼失败", None)
    return greeted


def step_reply(driver: SeekerDriver) -> int:
    """处理 HR 未读消息。"""
    profile = load_profile()
    handled = 0
    counted: set[int] = set()   # 同一职位多轮对话只计一次回复
    for ext_id in driver.fetch_unread()[:8]:
        row = _find_row_by_ext(ext_id)
        if row is None:
            continue
        job = _row_to_job(row)
        jid = int(row["id"])

        msgs = driver.read_messages(ext_id)
        if not msgs:
            continue

        # 只处理最后一条 HR 消息，避免把历史全部重放
        incoming = ""
        for m in reversed(msgs):
            if m.get("role") == "them":
                incoming = m.get("text", "")
                break
        if not incoming:
            continue

        # 已经处理过同一条就不再重复回（按内容比对）
        history = get_seek_messages(jid, limit=200)
        if any(h.get("role") == "them" and h.get("content") == incoming for h in history):
            continue

        turn = dialog_mod.handle(row, job, incoming, profile)
        if jid not in counted:
            bump_seek_stat("replied")
            counted.add(jid)

        if turn.action == "skip":
            continue

        _sleep("reply_interval")
        if turn.reply:
            driver.send_message(ext_id, turn.reply)

        if turn.action == "send_resume":
            if driver.send_resume(ext_id):
                update_seek_job(jid, state="resume_sent", resume_sent=1)
                bump_seek_stat("resumes")
                log_event("info", "seeker", f"「{job.title}」简历已发送", None)
            else:
                update_seek_job(jid, state="resume_manual")
                log_event("warn", "seeker",
                          f"「{job.title}」要简历但发送入口没找到，需人工发", None)
        elif turn.action == "accept_interview":
            bump_seek_stat("interviews")
        elif turn.action == "close":
            bump_seek_stat("rejected")

        handled += 1
    return handled


def step_resume_fallback(driver: SeekerDriver) -> int:
    """兜底：聊天中且策略允许时主动发简历。"""
    profile = load_profile()
    if str(profile.resume.get("send_policy", "ask")) != "after":
        return 0
    sent = 0
    for row in seek_jobs_by_state(["chatting"], limit=5):
        jid = int(row["id"])
        rounds = sum(1 for m in get_seek_messages(jid, limit=200) if m.get("role") == "them")
        need = int(profile.resume.get("send_after_rounds", 3) or 3)
        if rounds < need or int(row.get("resume_sent") or 0):
            continue
        ext = str(row.get("ext_id"))
        if driver.send_resume(ext):
            update_seek_job(jid, state="resume_sent", resume_sent=1)
            bump_seek_stat("resumes")
            sent += 1
    return sent


# ---------------- tick ----------------

def tick(once: bool = False) -> dict[str, Any]:
    if not once and (not _state["running"] or _state["paused"]):
        return {"skipped": True, "reason": "未运行或已暂停"}

    if not _in_window():
        return {"skipped": True, "reason": "不在运行时段"}

    started = time.time()
    summary: dict[str, Any] = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "collected": 0, "matched": 0, "greeted": 0, "replied": 0, "resumes": 0,
        "stopped": None,
    }

    driver = _driver()

    # 熔断优先
    signal = driver.risk_signal()
    if signal:
        summary["stopped"] = signal
        log_event("error", "risk", f"检测到 {signal}，已停止求职端运行")
        with _LOCK:
            _state["stop_reason"] = signal
            if signal in ("captcha", "risk"):
                _state["running"] = False
        return summary

    try:
        # 回消息优先于找新机会
        summary["replied"] = step_reply(driver)
        summary["resumes"] = step_resume_fallback(driver)
        summary["matched"] = step_match(limit=8)
        if _quota_left() > 0:
            summary["collected"] = step_collect(driver, limit=15)
            summary["greeted"] = step_greet(driver, limit=3)
    except Exception as exc:
        log_event("error", "seeker", f"tick 异常：{exc}")
        summary["error"] = str(exc)

    summary["elapsed_sec"] = round(time.time() - started, 1)
    summary["stats"] = seek_today_stats()
    with _LOCK:
        _state["last_tick"] = summary["at"]
        _state["tick_count"] = int(_state.get("tick_count", 0)) + 1
    return summary


# ---------------- 控制 ----------------

def start() -> dict[str, Any]:
    with _LOCK:
        _state["running"] = True
        _state["paused"] = False
        _state["stop_reason"] = None
    try:
        _driver()
    except Exception as exc:
        log_event("error", "seeker", f"驱动启动失败：{exc}")
        with _LOCK:
            _state["last_error"] = str(exc)
    log_event("info", "seeker", f"求职端开始运行（模式：{config.mode}）")
    return get_state()


def pause() -> dict[str, Any]:
    with _LOCK:
        _state["paused"] = True
    return get_state()


def resume() -> dict[str, Any]:
    with _LOCK:
        _state["paused"] = False
        _state["running"] = True
    return get_state()


def stop() -> dict[str, Any]:
    with _LOCK:
        _state["running"] = False
    return get_state()


# ---------------- 辅助 ----------------

def _row_to_job(row: dict[str, Any]) -> JobInfo:
    return JobInfo(
        ext_id=str(row.get("ext_id") or ""),
        title=str(row.get("title") or ""),
        company=str(row.get("company") or ""),
        salary_text=str(row.get("salary") or ""),
        salary_min=row.get("salary_min"),
        salary_max=row.get("salary_max"),
        city=str(row.get("city") or ""),
        district=str(row.get("district") or ""),
        tags=list(row.get("tags") or []),
        url=str(row.get("url") or ""),
        jd_text=str(row.get("jd_text") or ""),
    )


def _find_row_by_ext(ext_id: str) -> dict[str, Any] | None:
    """按 ext_id 找职位。

    mock 模式 ext_id 直接一致；真实模式下会话 id 是 idxN（列表序号），
    需要按序号回查——这里用"已打招呼且顺序一致"近似，仍有错配风险，
    所以真实模式的回消息能力需要在拿到账号后单独校准。
    """
    row = get_seek_job_by_ext(ext_id)
    if row:
        return row
    if ext_id.startswith("idx"):
        try:
            idx = int(ext_id.replace("idx", ""))
        except Exception:
            return None
        rows = seek_jobs_by_state(["greeted", "chatting", "resume_sent", "interview"], limit=50)
        if idx < len(rows):
            return rows[idx]
    return None
