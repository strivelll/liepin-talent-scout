"""对话状态机。

设计原则（对应方案 2.5.5）：**模型负责"想"，脚本负责"做"。**

- 什么时候问什么、什么时候该判定、什么时候转人工 —— 由脚本按状态推进，确定性可控；
- 怎么把问题问得自然、怎么回应候选人的反问 —— 交给模型。

这样既不会因为模型抽风把流程带偏，也不会因为硬编码模板让对话变成机器腔。
"""

from __future__ import annotations

from typing import Any

from ..config import JobConfig, config
from ..db import add_message, get_messages, log_event, update_candidate
from ..llm import llm


def flow_steps(job: JobConfig) -> list[dict[str, Any]]:
    return list((job.chat or {}).get("flow") or [])


def next_step(job: JobConfig, chat_step: int) -> dict[str, Any] | None:
    """取第 chat_step 步；已走完返回 None。"""
    steps = flow_steps(job)
    if 0 <= chat_step < len(steps):
        return steps[chat_step]
    return None


def required_count(job: JobConfig) -> int:
    """必答项数量——达到这个数就可以进入判定。"""
    return sum(1 for s in flow_steps(job) if s.get("required"))


def build_transcript(messages: list[dict[str, Any]]) -> str:
    """把消息列表拼成带说话人的文本，供模型判定使用。"""
    lines = []
    for m in messages:
        who = "候选人" if m.get("role") == "them" else "招聘方"
        lines.append(f"{who}：{m.get('content', '')}")
    return "\n".join(lines)


def should_judge(job: JobConfig, chat_step: int, round_count: int) -> bool:
    """是否该收口判定。

    两个条件任一满足即可：问答走完 或 轮次用尽。
    后者是为了避免候选人答非所问时无限追问。
    """
    cfg = config.chat_cfg
    max_rounds = int(cfg.get("max_rounds", 8))
    if chat_step >= len(flow_steps(job)):
        return True
    return round_count >= max_rounds


def handle_incoming(candidate: dict[str, Any], job: JobConfig, incoming_text: str) -> dict[str, Any]:
    """处理一条候选人消息，返回本轮的处置结论。

    返回结构：
      action: reply / stop_reject / stop_handover / advance
      reply:  需要发出去的文本（action=reply 时）
      judge:  是否应进入判定
    """
    cid = int(candidate["id"])
    name = str(candidate.get("name") or "")
    chat_step = int(candidate.get("chat_step") or 0)

    add_message(cid, "them", incoming_text)
    intent = llm.classify_intent(incoming_text)

    # ---- 明确拒绝：体面收尾，不再纠缠 ----
    if intent == "reject":
        reply = "好的，理解，那就不打扰了。如果后面有想法随时找我，祝顺利。"
        add_message(cid, "us", reply, intent="rejected")
        update_candidate(cid, state="rejected", reject_reason="候选人明确拒绝")
        log_event("info", "chat", f"{name} 明确拒绝，流程终止", cid)
        return {"action": "stop_reject", "reply": reply, "judge": False}

    # ---- 命中敏感话题：立即转人工，不让模型继续谈 ----
    if intent == "handover":
        update_candidate(cid, state="handed_over")
        log_event("warn", "chat", f"{name} 提到敏感话题，已转人工处理", cid)
        return {
            "action": "stop_handover",
            "reply": None,
            "judge": False,
            "reason": "涉及薪资/合同等细节，需人工对接",
        }

    # ---- 正常推进 ----
    # 首次收到回复即视为进入沟通中，供漏斗统计与调度使用
    if str(candidate.get("state") or "") == "greeted":
        update_candidate(cid, state="chatting")

    step = next_step(job, chat_step)
    history = get_messages(cid, limit=int(config.chat_cfg.get("context_window", 20)))
    round_count = sum(1 for m in history if m.get("role") == "them")

    # 该问什么：优先问下一步；若已问完，就自然地回应
    if step is not None:
        question = str(step.get("ask") or "")
    else:
        question = "看候选人还有没有其他想问的，简短回应即可"

    reply = llm.generate_reply(
        persona=str((job.chat or {}).get("persona") or ""),
        company_intro=str((job.chat or {}).get("company_intro") or ""),
        history=history,
        question=question,
        candidate_name=name,
    )

    # 只有在"确认收到回答"时才推进步骤：
    # 反问（question）不推进，因为对方还没回答当前问题
    advanced = 0
    if intent in ("answer", "empty") and step is not None:
        advanced = 1
        update_candidate(cid, chat_step=chat_step + 1)
        chat_step += 1

    add_message(cid, "us", reply)
    log_event("info", "chat", f"{name} 第 {round_count} 轮，意图={intent}，推进={advanced}", cid)

    return {
        "action": "reply",
        "reply": reply,
        "judge": should_judge(job, chat_step, round_count),
        "intent": intent,
        "step_advanced": bool(advanced),
    }


def do_judge(candidate: dict[str, Any], job: JobConfig) -> dict[str, Any]:
    """收口判定：跑模型给分，落状态。"""
    cid = int(candidate["id"])
    name = str(candidate.get("name") or "")
    messages = get_messages(cid, limit=200)
    transcript = build_transcript(messages)
    criteria = str((job.judgement or {}).get("criteria") or "")

    # 岗位级阈值优先，其次全局阈值——这是全系统唯一的阈值来源
    threshold = int((job.judgement or {}).get("pass_threshold", 0)) or int(
        config.invite_cfg.get("trigger_score", 70)
    )
    score, verdict, reason = llm.judge_candidate(criteria, transcript, threshold)

    state = "qualified" if verdict == "qualified" else "rejected"
    update_candidate(cid, state=state, score=score, score_reason=reason,
                     reject_reason=None if verdict == "qualified" else reason)
    log_event(
        "info", "judge",
        f"{name} 判定 {verdict}，{score} 分，{reason}",
        cid,
    )
    return {"verdict": verdict, "score": score, "reason": reason, "state": state}


def greeting_text(job: JobConfig, candidate: dict[str, Any], reason: str = "") -> str:
    """组装招呼语：从岗位配置的话术库里轮换，并注入变量。

    轮换用的是"候选人 id 取模"而不是随机——保证同一候选人多次重试拿到同一句话，
    避免重复发送时内容不一致。
    """
    ids = list((job.greeting or {}).get("script_ids") or [])
    fallback = config.get_script("generic_a")
    if not ids:
        template = fallback
    else:
        cid = int(candidate.get("id") or 0)
        template = config.get_script(ids[cid % len(ids)]) or fallback

    name = str(candidate.get("name") or "")
    highlight = ""
    tags = candidate.get("tags") or "[]"
    try:
        import json as _json

        parsed = _json.loads(tags) if isinstance(tags, str) else tags
        if isinstance(parsed, list) and parsed:
            highlight = str(parsed[0])
    except Exception:
        highlight = ""

    return (
        template.replace("{name}", name or "你好")
        .replace("{job}", job.name)
        .replace("{company}", "我们")
        .replace("{reason}", reason or f"你的背景和{job.name}比较匹配")
        .replace("{highlight}", highlight or "相关")
    )
