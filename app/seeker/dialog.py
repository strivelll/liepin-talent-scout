"""求职端对话状态机。

和招聘端是镜像关系：

    招聘端：候选人怎么答 → 判定是否合格
    求职端：HR 怎么问  → 决定怎么答、要不要发简历

**意图识别用规则优先，模型兜底。** 原因：
- HR 的意图高度集中在五类，关键词足以覆盖
- 规则可解释、零成本、毫秒级，适合高频路径
- 只有规则判不出来（"我们这边待遇挺灵活的"）才交给模型

**要简历是最关键的一类。** 它不只要回复，还要触发动作（发简历），
所以单独作为一个 action 处理，不能混在普通回复里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import config
from ..db import add_seek_message, get_seek_messages, log_event, update_seek_job
from .matcher import JobInfo
from .profile import Profile
from . import scripts as script_gen


# 关键词表。**顺序即优先级**，这个顺序被踩过坑：
#
# 1. reject 必须最前 —— "可能不太合适""祝你找到合适的机会"都是婉拒，
#    如果前面先命中 ask_experience（因为句子里有"经验"），就会对着拒信继续推销。
# 2. invite_interview 要排在 want_resume 前 —— HR 说"简历看过了，什么时候方便面试"，
#    真实意图是约面试，不是要简历。早期版本把"简历"当信号，回了一段发简历的话，答非所问。
# 3. want_resume 必须匹配"要的动作"，不能只看"简历"两个字。
#    "简历看过了""收到简历"都是在陈述，不是在要。
RULES: list[tuple[str, list[str]]] = [
    ("reject", ["不合适", "不太合适", "已招到", "招满了", "暂不考虑", "不符合",
                "抱歉", "祝你找到", "合适的机会", "有更合适", "暂时不需要", "岗位关闭"]),
    ("invite_interview", ["面试", "面谈", "来公司", "什么时候方便", "约个时间",
                          "地址", "几点", "到岗时间", "聊聊时间"]),
    ("want_resume", ["发一份简历", "发份简历", "发我一份", "简历发我", "发一下简历",
                     "麻烦发", "看一下简历", "看下简历", "附上简历", "发个简历"]),
    ("ask_salary", ["薪资", "工资", "待遇", "期望多少", "多少钱", "几k", "底薪", "package"]),
    ("ask_experience", ["经验", "做过", "会不会", "几年", "擅长", "了解过", "做过什么"]),
    ("greeting", ["你好", "在吗", "看到你的", "方便聊"]),
    # 对方说"我看下再回复"——这时候追问会显得急，简短确认即可
    ("waiting", ["给你回复", "我看下", "稍后回复", "回头联系", "有消息通知", "再联系你"]),
]

# 婉拒类话术：常出现在对话结尾，规则没覆盖时会误判成"其他"继续追问
CLOSING_HINTS = ["祝你", "顺利", "再联系", "保持联系", "后续有"]

# 系统消息特征：BOSS 会在会话里插入这类提示，不是 HR 说的话
SYSTEM_HINTS = ["竞争者", "PK情况", "投递", "建议你", "查看详细分析", "简历已送达"]


@dataclass
class DialogTurn:
    """一轮对话的处理结果。"""

    intent: str
    action: str                 # reply / send_resume / accept_interview / close / skip
    reply: str = ""
    state: str = ""             # 处理后的职位状态
    note: str = ""


def is_system_message(text: str) -> bool:
    return any(h in (text or "") for h in SYSTEM_HINTS)


def classify(text: str) -> str:
    """意图分类。规则命中即返回，全不中交给模型。"""
    low = (text or "").strip().lower()
    if not low:
        return "other"

    for intent, words in RULES:
        if any(w in low for w in words):
            # "简历"出现在"简历已收到"这种系统话术里时不算要简历
            if intent == "want_resume" and is_system_message(low):
                continue
            return intent

    # 兜底：像结尾话术的一律按婉拒处理，别再追问
    if any(h in low for h in CLOSING_HINTS) and len(low) < 40:
        return "reject"

    # 规则没命中，问句形态的交给模型判断
    if llm_available() and (low.endswith("?") or low.endswith("？") or len(low) > 20):
        return _llm_classify(text)
    return "other"


def llm_available() -> bool:
    from ..llm import llm

    return llm.available


def _llm_classify(text: str) -> str:
    from ..llm import llm

    system = "你是对话意图分类器。只输出一个标签，不要任何解释。"
    user = (
        f"HR 说：{text}\n\n"
        "判断属于哪一类，从下面选一个标签输出：\n"
        "want_resume（要简历）/ ask_salary（问薪资）/ ask_experience（问经验能力）"
        " / invite_interview（约面试）/ reject（拒绝）/ greeting（打招呼）/ other（其他）"
    )
    try:
        res = llm._chat(system, user)
        label = (res.text or "").strip().split()[0].strip("，。,.")
        valid = {"want_resume", "ask_salary", "ask_experience", "invite_interview",
                 "reject", "greeting", "other"}
        return label if label in valid else "other"
    except Exception:
        return "other"


def handle(
    job_row: dict[str, Any],
    job: JobInfo,
    incoming: str,
    profile: Profile,
) -> DialogTurn:
    """处理一条 HR 消息，返回该做什么。"""
    jid = int(job_row["id"])

    # 系统提示不参与对话，但要落库以便追溯
    if is_system_message(incoming):
        add_seek_message(jid, "system", incoming)
        return DialogTurn(intent="system", action="skip", state="", note="系统消息，跳过")

    # 已经结束的对话不再回应。HR 有时会连发两条（"不太合适" + "祝你顺利"），
    # 如果每条都回一遍礼貌收尾，反而显得像机器人在刷屏。
    if str(job_row.get("state")) in ("rejected", "closed"):
        add_seek_message(jid, "them", incoming)
        return DialogTurn(intent="closed", action="skip", state="rejected",
                          note="对话已结束，不再回复")

    add_seek_message(jid, "them", incoming)
    intent = classify(incoming)

    # 2) 上下文修正：已经在约面试阶段，对方说时间就是在确认
    # "明天下午三点可以吗" 这类句子没有"面试"字样，纯关键词会判成 other
    if intent == "other" and str(job_row.get("state")) == "interview":
        if any(w in incoming for w in ("点", "明天", "后天", "上午", "下午", "可以吗", "方便", "地址")):
            intent = "invite_interview"
    history = get_seek_messages(jid, limit=40)

    # ---- 拒绝：礼貌收尾，不再纠缠 ----
    if intent == "reject":
        text = script_gen.reply(job, incoming, "reject", profile, history)
        add_seek_message(jid, "me", text, intent="reject")
        update_seek_job(jid, state="rejected", reject_why=incoming[:60])
        log_event("info", "seeker", f"「{job.title}」被拒：{incoming[:40]}", None)
        return DialogTurn(intent=intent, action="close", reply=text, state="rejected")

    # ---- 要简历：回复 + 触发发简历动作 ----
    if intent == "want_resume":
        note = script_gen.resume_note(profile)
        add_seek_message(jid, "me", note, intent="want_resume")
        update_seek_job(jid, state="resume_pending")
        log_event("info", "seeker", f"「{job.title}」HR 要简历，准备发送", None)
        return DialogTurn(
            intent=intent, action="send_resume", reply=note,
            state="resume_pending", note="需要发送简历",
        )

    # ---- 约面试：确认并记录 ----
    if intent == "invite_interview":
        text = script_gen.reply(job, incoming, intent, profile, history)
        add_seek_message(jid, "me", text, intent=intent)
        update_seek_job(jid, state="interview")
        log_event("info", "seeker", f"「{job.title}」进入面试沟通", None)
        return DialogTurn(intent=intent, action="accept_interview", reply=text, state="interview")

    # ---- 其他：正常回复 ----
    text = script_gen.reply(job, incoming, intent, profile, history)
    add_seek_message(jid, "me", text, intent=intent)

    # 聊够轮次且策略是 after 时，主动发简历
    state = str(job_row.get("state") or "chatting")
    if _should_auto_send(jid, profile, job_row):
        update_seek_job(jid, state="resume_pending")
        return DialogTurn(intent=intent, action="send_resume", reply=text,
                          state="resume_pending", note="聊够轮次，主动发简历")

    update_seek_job(jid, state="chatting")
    return DialogTurn(intent=intent, action="reply", reply=text, state="chatting")


def _should_auto_send(jid: int, profile: Profile, job_row: dict[str, Any]) -> bool:
    """判断是否该主动发简历。"""
    policy = str(profile.resume.get("send_policy", "ask"))
    if policy != "after":
        return False
    if int(job_row.get("resume_sent") or 0):
        return False
    rounds = sum(1 for m in get_seek_messages(jid, limit=200) if m.get("role") == "them")
    need = int(profile.resume.get("send_after_rounds", 3) or 3)
    return rounds >= need


def stats() -> dict[str, int]:
    """对话相关统计，给界面用。"""
    from ..db import get_conn

    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(DISTINCT job_id) AS n FROM seek_messages WHERE role='them'"
    ).fetchone()
    return {"replied_jobs": int(row["n"]) if row else 0}
