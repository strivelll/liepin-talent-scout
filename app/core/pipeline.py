"""主流程编排：一次 tick 走完一轮完整决策。

tick 的执行顺序是有讲究的：

1. 风控检查 —— 有异常立即全停，不做任何操作（重试是封号的头号原因）
2. 运行时段 —— 不在允许的时段直接返回
3. 处理未读消息 —— **聊天优先于打招呼**，有回复先回，这是清单 4.14 项的要求
4. 收口判定 —— 对已聊完的候选人打分
5. 面试邀约 —— 合格者发邀约
6. 打招呼 —— 还有额度才做
7. 拉新候选人 —— 还有余额才拉

把"聊天"排在"打招呼"前面，是因为已回复的候选人价值远高于新候选人，
而平台的会话窗口是有限的。
"""

from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime, time as dtime
from typing import Any

from ..browser import get_driver
from ..browser.base import BrowserDriver, Card
from ..config import JobConfig, config
from ..db import (
    add_message,
    bump_stat,
    candidates_by_state,
    count_today_greeted,
    get_candidate,
    get_messages,
    list_candidates,
    log_event,
    today_stats,
    update_candidate,
    upsert_candidate,
)
from . import chat as chat_mod
from .filter import run_funnel

_STATE_LOCK = threading.RLock()
_DRIVER: BrowserDriver | None = None
_DRIVER_LOCK = threading.Lock()

# 运行态
_state: dict[str, Any] = {
    "running": False,
    "paused": False,
    "last_tick": None,
    "last_error": None,
    "stop_reason": None,
    "greeted_today": 0,
    "tick_count": 0,
}


def get_state() -> dict[str, Any]:
    with _STATE_LOCK:
        snap = dict(_state)
    snap["greeted_today"] = count_today_greeted()
    snap["stats"] = today_stats()
    return snap


def _driver() -> BrowserDriver:
    """懒加载并复用驱动实例。"""
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is None:
            _DRIVER = get_driver()
            _DRIVER.open()
        return _DRIVER


def reset_driver() -> None:
    """换模式或异常后重建驱动。"""
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is not None:
            try:
                _DRIVER.close()
            except Exception:
                pass
        _DRIVER = None


# ---------------- 节奏与时段 ----------------

def in_run_window() -> bool:
    """判断当前是否在允许运行的时段内。"""
    window = config.pacing.get("run_window") or ["00:00", "23:59"]
    try:
        start = datetime.strptime(str(window[0]), "%H:%M").time()
        end = datetime.strptime(str(window[1]), "%H:%M").time()
    except Exception:
        return True
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    # 跨零点
    return now >= start or now <= end


def _sleep_between(kind: str) -> None:
    """按配置做随机等待。用 uniform 而非固定值，避免规律性间隔。

    Mock 模式直接压缩到毫秒级：这些延迟是为了在真实平台上"装得像人"，
    不连平台时没有意义，反而让演示等得难受。
    """
    if config.mode != "real":
        time.sleep(random.uniform(0.02, 0.12))
        return
    lo, hi = config.pacing.get(kind, [1, 2])
    time.sleep(random.uniform(float(lo), float(hi)))


def _daily_quota_left(job: JobConfig) -> int:
    """剩余打招呼额度 = min(全局日上限, 岗位日上限) - 今日已发。"""
    global_cap = int(config.pacing.get("daily_greet_limit", 40))
    job_cap = int((job.greeting or {}).get("max_per_day", global_cap))
    cap = min(global_cap, job_cap)
    return max(0, cap - count_today_greeted())


# ---------------- 各环节 ----------------

def step_risk_check(driver: BrowserDriver) -> str | None:
    """风控检查。返回非空表示应当停止本轮。"""
    signal = driver.risk_signal()
    if not signal:
        return None
    shot = driver.screenshot(f"risk_{signal}")
    if signal == "captcha":
        log_event("error", "risk", f"检测到验证码，已停止运行。截图：{shot}")
    elif signal == "daily_limit":
        log_event("warn", "risk", "平台提示今日沟通已达上限，今日不再打招呼")
    elif signal == "login_expired":
        log_event("error", "risk", "登录态失效，需人工重新登录。截图：" + str(shot))
    else:
        log_event("warn", "risk", f"检测到风控信号：{signal}，已暂停。截图：{shot}")
    with _STATE_LOCK:
        _state["stop_reason"] = signal
        if signal in ("captcha", "login_expired"):
            _state["running"] = False
    return signal


def step_handle_replies(driver: BrowserDriver, limit: int = 6) -> int:
    """处理未读会话：读消息 → 状态机处置 → 回复 → 必要时判定/邀约。

    关键点：**必须按"已入库的对方消息条数"对齐增量**。
    平台返回的永远是整个会话历史，如果直接遍历就会把同一条消息反复处理，
    导致回复数虚高、重复判定、重复发邀约。这里用 db 里已有的 them 消息数
    作为游标，只消费新增的那几条。
    """
    handled = 0
    for ext_id in driver.fetch_unread()[:limit]:
        candidate = _find_by_ext(ext_id)
        if candidate is None:
            continue

        job = config.job(str(candidate["job_id"]))
        if job is None:
            continue

        cid = int(candidate["id"])
        db_msgs = get_messages(cid, limit=500)
        seen_them = sum(1 for m in db_msgs if m.get("role") == "them")

        live = driver.read_messages(ext_id, limit=200)
        fresh = live[seen_them:] if len(live) > seen_them else []
        if not fresh:
            continue

        # 一轮最多消化 2 条新消息，避免刷屏式连续回复
        for msg in fresh[:2]:
            result = chat_mod.handle_incoming(candidate, job, msg.text)
            bump_stat("replied")

            if result["action"] == "reply" and result.get("reply"):
                if config.chat_cfg.get("reply_delay_simulate", True):
                    _sleep_between("reply_interval_sec")
                driver.send_message(ext_id, str(result["reply"]))
            elif result["action"] == "stop_reject" and result.get("reply"):
                driver.send_message(ext_id, str(result["reply"]))
                bump_stat("rejected")
            elif result["action"] == "stop_handover":
                bump_stat("handover")
                log_event("warn", "chat", f"{candidate['name']} 已转人工，请尽快跟进", cid)
                break

            candidate = get_candidate(cid) or candidate

            if result.get("judge"):
                _judge_and_invite(driver, candidate, job)
                break

        handled += 1
    return handled


def step_judge_pending(driver: BrowserDriver, job_id: str | None = None) -> int:
    """兜底：对卡在 chatting 但已走完剧本的候选人补一次判定。

    传入 job_id 做岗位过滤——早期版本不带参数，导致外层每遍历一个岗位
    就把全表所有 chatting 候选人重扫一遍，重复判定。
    """
    done = 0
    for cand in candidates_by_state(["chatting"], job_id=job_id):
        job = config.job(str(cand["job_id"]))
        if job is None:
            continue
        step = int(cand.get("chat_step") or 0)
        rounds = len(get_messages(int(cand["id"]), limit=200))
        if chat_mod.should_judge(job, step, rounds):
            _judge_and_invite(driver, cand, job)
            done += 1
        if done >= 5:
            break
    return done


def _judge_and_invite(driver: BrowserDriver, candidate: dict[str, Any], job: JobConfig) -> None:
    """判定并按结果决定是否发邀约。

    **必须幂等**：同一个人可能被"处理回复"和"兜底扫描"两条路径同时选中，
    如果没有状态守卫就会判定两次、邀约发两遍、统计翻倍。
    这里以数据库里的最新状态为准，已判定过的一律跳过。
    """
    cid = int(candidate["id"])
    fresh = get_candidate(cid)
    if fresh is None:
        return
    if str(fresh.get("state")) not in ("chatting", "greeted"):
        return  # 已判定过（qualified / invited / rejected / handed_over），直接跳过

    verdict = chat_mod.do_judge(fresh, job)

    if verdict["verdict"] != "qualified":
        bump_stat("rejected")
        return

    bump_stat("qualified")
    if not config.invite_cfg.get("enabled", True):
        return

    slot = _pick_slot()
    if not slot:
        log_event("warn", "invite", f"{candidate['name']} 已合格，但无可约时段", cid)
        return

    address = str(config.invite_cfg.get("address", "线上面试"))
    duration = int(config.invite_cfg.get("duration_min", 30))
    text = (
        f"{candidate['name']}你好，聊下来感觉挺合适的。"
        f"我们想约你做个 {duration} 分钟的面试，{slot} 方便吗？{address}。"
        "如果时间不合适，你说个方便的时间我来协调。"
    )
    if driver.send_message(str(candidate["ext_id"]), text):
        add_message(cid, "us", text, intent="invite")
        update_candidate(cid, state="invited", invite_slot=slot,
                         invite_sent_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        bump_stat("invited")
        log_event("info", "invite", f"{candidate['name']} 已发面试邀约：{slot}", cid)


def _pick_slot() -> str | None:
    """从配置时段里挑一个尚未被占用的。"""
    slots = list(config.invite_cfg.get("slots") or [])
    if not slots:
        return None
    used = {
        str(c.get("invite_slot"))
        for c in list_candidates(state="invited", limit=500)
        if c.get("invite_slot")
    }
    for s in slots:
        if s not in used:
            return str(s)
    return None


def step_greet(driver: BrowserDriver, job: JobConfig, limit: int = 5) -> int:
    """拉候选人 → 跑漏斗 → 打招呼。"""
    quota = _daily_quota_left(job)
    if quota <= 0:
        return 0

    cards: list[Card] = driver.fetch_candidates(job.id, limit=max(10, limit * 3))
    greeted = 0

    for card in cards:
        if greeted >= limit or greeted >= quota:
            break
        if driver.risk_signal():
            break

        data = card.to_dict()
        cid = upsert_candidate(job.id, data)

        # 已处理过的跳过（去重的第一重：数据库唯一键）
        existing = get_candidate(cid) or {}
        if existing.get("state") not in ("pending",):
            continue

        funnel = run_funnel(card, job)
        if not funnel["passed"]:
            update_candidate(cid, state="rejected_filter", score=int(funnel["score"]),
                             score_reason=str(funnel["reason"]))
            log_event("info", "judge",
                      f"{card.name} 筛选未通过（{funnel['stage']}）：{funnel['reason']}", cid)
            continue

        update_candidate(cid, state="prescreened", score=int(funnel["score"]),
                         score_reason=str(funnel["reason"]))

        text = chat_mod.greeting_text(job, {**data, "id": cid}, str(funnel["reason"]))
        _sleep_between("greet_interval_sec")

        if driver.greet(card, text):
            add_message(cid, "us", text)
            update_candidate(cid, state="greeted", greeting_text=text)
            bump_stat("greeted")
            greeted += 1
            log_event("info", "greet", f"已向 {card.name} 打招呼", cid)
        else:
            update_candidate(cid, state="failed", reject_reason="打招呼发送失败")
            log_event("warn", "greet", f"{card.name} 打招呼失败，已标记", cid)

    return greeted


def step_advance_chat(driver: BrowserDriver, job: JobConfig, limit: int = 3) -> int:
    """主动推进：把 greeted 状态但对方还没回的会话，最多等一轮；已回的转 chatting。"""
    moved = 0
    for cand in candidates_by_state(["greeted"], job_id=job.id):
        cid = int(cand["id"])
        msgs = get_messages(cid, limit=100)
        if any(m.get("role") == "them" for m in msgs):
            update_candidate(cid, state="chatting")
            moved += 1
        if moved >= limit:
            break
    return moved


# ---------------- 一次 tick ----------------

def tick(once: bool = False) -> dict[str, Any]:
    """执行一轮完整决策。返回本轮摘要。"""
    if not once and (not _state["running"] or _state["paused"]):
        return {"skipped": True, "reason": "未运行或已暂停"}

    started = time.time()
    summary: dict[str, Any] = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "replied": 0, "greeted": 0, "judged": 0, "advanced": 0, "stopped": None,
    }

    if not in_run_window():
        summary["skipped"] = True
        summary["reason"] = "不在运行时段"
        return summary

    driver = _driver()

    # 1. 风控
    signal = step_risk_check(driver)
    if signal:
        summary["stopped"] = signal
        return summary

    # 2. 聊天优先
    try:
        summary["replied"] = step_handle_replies(driver)
    except Exception as exc:
        log_event("error", "chat", f"处理回复异常：{exc}")
        summary["error"] = str(exc)

    for job in config.active_jobs():
        if driver.risk_signal():
            break
        try:
            summary["advanced"] += step_advance_chat(driver, job)
            summary["judged"] += step_judge_pending(driver, job.id)
            summary["greeted"] += step_greet(driver, job, limit=3)
        except Exception as exc:
            log_event("error", "system", f"岗位 {job.id} 执行异常：{exc}")
            summary["error"] = str(exc)

    summary["elapsed_sec"] = round(time.time() - started, 1)
    summary["stats"] = today_stats()

    with _STATE_LOCK:
        _state["last_tick"] = summary["at"]
        _state["tick_count"] = int(_state.get("tick_count", 0)) + 1

    return summary


# ---------------- 控制 ----------------

def start() -> dict[str, Any]:
    with _STATE_LOCK:
        _state["running"] = True
        _state["paused"] = False
        _state["stop_reason"] = None
    # 预热驱动，早暴露登录/依赖问题
    try:
        _driver()
    except Exception as exc:
        log_event("error", "system", f"驱动启动失败：{exc}")
        with _STATE_LOCK:
            _state["last_error"] = str(exc)
    log_event("info", "system", f"开始运行（模式：{config.mode}）")
    return get_state()


def pause() -> dict[str, Any]:
    with _STATE_LOCK:
        _state["paused"] = True
    log_event("info", "system", "已暂停")
    return get_state()


def resume() -> dict[str, Any]:
    with _STATE_LOCK:
        _state["paused"] = False
        _state["running"] = True
    log_event("info", "system", "已恢复")
    return get_state()


def stop() -> dict[str, Any]:
    with _STATE_LOCK:
        _state["running"] = False
    log_event("info", "system", "已停止")
    return get_state()


def _find_by_ext(ext_id: str) -> dict[str, Any] | None:
    """mock 模式的 ext_id 是 m001 这种；真实模式是 job-idx-name。
    统一走一次全表扫描（量小，可接受），命中即返回。"""
    for cand in list_candidates(limit=2000):
        if str(cand.get("ext_id")) == ext_id:
            return cand
    # mock 的会话 id 只有 m001，而候选人 ext_id 是 job-idx-name，做一次前缀匹配
    for cand in list_candidates(limit=2000):
        if str(cand.get("ext_id", "")).endswith(ext_id):
            return cand
    return None
