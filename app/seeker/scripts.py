"""招呼语与回复文案生成。

核心原则：**话术的差异来自信息密度，不是措辞花哨。**

HR 一天收到几十条"您好，我对贵司岗位很感兴趣"，全部跳过。真正能换来回复的
只有一种：里面带了对方 JD 里的具体信息，或者自己一个可验证的成果。

所以招呼语的结构固定为三段：
    一个具体匹配点（来自 pitch） + 一句人话 + 一个开放问题

模板只负责骨架，内容必须来自匹配结果。轮换用"职位 id 取模"而不是随机，
保证同一岗位重试时话术一致（避免重复发送内容不同的消息）。
"""

from __future__ import annotations

import re

from typing import Any

from ..config import config
from ..llm import llm
from .matcher import JobInfo, MatchResult
from .profile import Profile


def _style() -> dict[str, Any]:
    return ((config.seeker or {}).get("profile", {}) or {}).get("style", {}) or {}


def _max_chars() -> int:
    return int(_style().get("max_chars", 60) or 60)


# ---------------- 招呼语 ----------------

GREET_TEMPLATES = [
    "看到{point}这个岗，我{proof}，方便聊下具体做什么吗？",
    "{point}方向我{proof}，想问下你们现在最看重什么？",
    "你好，我{proof}，看到在招{point}，想了解下技术要求",
]

# 从职位标题里提炼方向词。真实抓列表页拿不到 JD 正文，
# 所以不能只依赖 JD 关键词，否则会退化成"看到经验不限这个岗"这种废话。
TITLE_HINTS = ("Java", "后端", "前端", "全栈", "服务端", "开发", "算法", "测试",
               "数据", "运维", "产品", "运营", "设计")


def greeting(job: JobInfo, match: MatchResult, profile: Profile, seed: int = 0) -> str:
    """生成招呼语。

    seed 用职位 id，保证同一岗位多次生成结果一致。
    """
    point = _short_point(job, match)
    proof = _proof(match, profile)

    if llm.available and match.pitch:
        text = _llm_greeting(job, match, profile)
        if text:
            return _clip(text)

    tpl = GREET_TEMPLATES[seed % len(GREET_TEMPLATES)]
    text = tpl.format(point=point, proof=proof)
    return _clip(text)


def _short_point(job: JobInfo, match: MatchResult) -> str:
    """提炼一个能聊的具体点。

    优先级：JD 关键词 > 标题方向词 > 有效标签。
    标签要排除"经验不限/本科"这类条件词——用它们当开场等于没开场。
    """
    if job.jd_text:
        for kw in ("Spring", "Java", "后端", "数据库", "微服务", "并发",
                   "短视频", "新媒体", "内容", "私域", "社群", "投放", "增长"):
            if kw.lower() in job.jd_text.lower():
                return kw

    # 用职位标题的核心词，不要用通用方向词。
    # 如果用"Java"当 point，10 条招呼会长得一模一样，一眼就是群发。
    title = (job.title or "").strip()
    base = re.split(r"[（(]", title)[0].split("/")[0].strip()
    for suffix in ("开发工程师", "工程师", "程序员", "顾问", "专员", "助理", "经理", "主管"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].strip()
            break
    if base and len(base) >= 2:
        return base[:12]

    for kw in TITLE_HINTS:
        if kw.lower() in title.lower():
            return kw

    noise = ("经验", "学历", "本科", "大专", "硕士", "年", "薪", "不限", "优先")
    for t in job.tags:
        if t and not any(x in t for x in noise):
            return t

    return "这个岗位"


def _proof(match: MatchResult, profile: Profile) -> str:
    """挑一个可验证的成果作为证明。"""
    if match.pitch:
        return match.pitch
    ranked = profile.highlights_for(" ".join(match.matched))
    if ranked:
        return ranked[0].key
    return "有相关经验"


def _llm_greeting(job: JobInfo, match: MatchResult, profile: Profile) -> str:
    style = _style()
    system = (
        "你在 BOSS 直聘上给 HR 发第一条消息。"
        f"风格要求：{style.get('tone', '简洁自然')}。"
        f"字数不超过 {_max_chars()} 字。"
        "不要用'您好，我对贵司岗位非常感兴趣'这类模板句。"
        "必须提到对方 JD 里的一个具体点。只输出消息内容本身。"
    )
    user = (
        f"我的情况：{profile.summary()}\n\n"
        f"目标岗位：{job.as_jd()}\n\n"
        f"已识别到的匹配点：{match.pitch}\n"
        f"其他匹配项：{'、'.join(match.matched[:3])}\n\n"
        "写一条开场消息。要求：\n"
        "1. 第一句就给出具体信息，不要先客套\n"
        "2. 结尾问一个关于岗位的真实问题（不是'方便聊聊吗'这种空问）\n"
        "3. 像真人手机打字，不用书面语"
    )
    try:
        res = llm._chat(system, user)
        return (res.text or "").strip().strip('"').strip("“”")
    except Exception:
        return ""


def _clip(text: str) -> str:
    """截断到字数上限，并保证结尾是完整语气。"""
    limit = _max_chars()
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("，", "。", "？", "！", " "):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:
            return cut[: pos + 1]
    return cut


# ---------------- 回复文案 ----------------

INTENT_HINT = {
    "want_resume": "对方要简历。表示可以，说明会马上发，不要长篇大论。",
    "ask_salary": "对方问薪资。给出期望区间，留一点弹性，不要报死价。",
    "ask_experience": "对方在确认你的经验。挑最相关的一段讲，必须带具体数字或结果。",
    "invite_interview": "对方约面试。表示可以，确认时间方式，问清地址或线上链接。",
    "reject": "对方拒绝或岗位已招满。礼貌收尾，不纠缠、不追问原因。",
    "greeting": "对方打招呼。简短回应，并把话题引到岗位本身。",
    "waiting": "对方说看完再回复。简短确认，不要追问，把主动权留给对方。",
    "other": "对方在问其他问题。直接回答，如果信息不足就反问一句。",
}


def reply(
    job: JobInfo,
    incoming: str,
    intent: str,
    profile: Profile,
    history: list[dict[str, str]],
) -> str:
    """生成对 HR 的回复。"""
    if intent == "reject":
        return _clip("好的，理解，谢谢回复。后面如果有合适的岗位再联系，祝顺利。")

    if llm.available:
        text = _llm_reply(job, incoming, intent, profile, history)
        if text:
            return _clip(text)

    return _clip(_template_reply(intent, profile, incoming))


TIME_WORDS = ("点", "明天", "后天", "上午", "下午", "周一", "周二", "周三", "周四",
              "周五", "周末", "这周", "下周")


def _template_reply(intent: str, profile: Profile, incoming: str = "") -> str:
    lo, hi = profile.salary_min, profile.salary_max
    salary = f"{lo // 1000}-{hi // 1000}K" if lo and hi else "面议"
    top = profile.highlights[0].key if profile.highlights else "相关经验"

    # 约面试要分两种：对方问"什么时候方便" vs 对方已经给出具体时间。
    # 都用同一句会导致复读 —— 对方说"明天下午三点可以吗"，回"您看什么时间合适"就是答非所问。
    if intent == "invite_interview":
        if any(w in (incoming or "") for w in TIME_WORDS):
            return "可以的，那就这个时间。需要我带什么材料吗？"
        return "方便的，您看什么时间合适？线上还是到公司？"

    return {
        "want_resume": "可以的，我整理一下马上发您。",
        "ask_salary": f"期望 {salary} 左右，具体可以聊，主要看岗位职责。",
        "ask_experience": f"我{top}，这块是我做得最久的部分。",
        "greeting": "你好，方便的话想了解下这个岗位具体负责什么？",
        "waiting": "好的，等您消息，有问题随时问我。",
        "other": "明白，方便的话可以再具体说下这块吗？",
    }.get(intent, "好的，我了解了。")


def _llm_reply(
    job: JobInfo,
    incoming: str,
    intent: str,
    profile: Profile,
    history: list[dict[str, str]],
) -> str:
    style = _style()
    system = (
        "你在 BOSS 直聘上回复 HR。"
        f"风格：{style.get('tone', '简洁自然')}，像真人手机打字，不超过 {_max_chars()} 字。"
        "不要客套，不要用'感谢您的关注'这类话。只输出消息内容。"
    )
    lines = []
    for m in history[-8:]:
        who = "HR" if m.get("role") == "them" else "我"
        lines.append(f"{who}：{m.get('content', '')}")
    transcript = "\n".join(lines) or "（暂无历史）"

    user = (
        f"我的情况：{profile.summary()}\n\n"
        f"岗位：{job.title}｜{job.company}\n\n"
        f"对话记录：\n{transcript}\n\n"
        f"HR 刚说：{incoming}\n\n"
        f"这一轮的处理原则：{INTENT_HINT.get(intent, INTENT_HINT['other'])}\n\n"
        "写我要回复的那一条消息。"
    )
    try:
        res = llm._chat(system, user)
        return (res.text or "").strip().strip('"').strip("“”")
    except Exception:
        return ""


# ---------------- 简历发送语 ----------------

def resume_note(profile: Profile) -> str:
    """发简历时附的一句话。只说明"这是什么"，不要重复自我介绍。"""
    return "简历发您，有需要补充的随时问我。"
