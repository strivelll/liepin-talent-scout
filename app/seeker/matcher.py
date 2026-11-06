"""岗位匹配打分。

两级判断，和招聘端的漏斗思路一致，只是方向反过来：

1. **硬筛**（零成本）：城市、薪资区间、职位名关键词、一票否决词。
   不满足直接跳过，不浪费招呼额度 —— 求职端每天能打的招呼也是有限的。
2. **软筛**（模型）：技能是否做得了、经验层级是否对等、有没有硬伤。
   同时产出一句 `pitch`，这是招呼语的"钩子"，必须引用 JD 或简历里的具体事实。

为什么 pitch 必须由模型生成而不是套模板：
模板招呼 HR 一天能收到几十条，一眼就认出来。真正能换来回复的是
"你 JD 里提到 X，我之前做过 Y"这种带具体信息的开场。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .. import boss_font
from ..config import config
from ..llm import llm
from .profile import Profile


@dataclass
class JobInfo:
    """岗位信息。字段来自列表页 + （可选）详情页 JD。"""

    ext_id: str
    title: str = ""
    company: str = ""
    salary_text: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    city: str = ""
    district: str = ""
    tags: list[str] = field(default_factory=list)
    url: str = ""
    jd_text: str = ""

    def as_jd(self) -> str:
        """拼成给模型看的岗位描述。"""
        parts = [
            f"标题：{self.title}",
            f"公司：{self.company}" if self.company else "",
            f"薪资：{self.salary_text}" if self.salary_text else "",
            f"地点：{self.city}{self.district}" if (self.city or self.district) else "",
            f"标签：{'、'.join(self.tags)}" if self.tags else "",
            f"描述：{self.jd_text}" if self.jd_text else "",
        ]
        return "\n".join(p for p in parts if p)

    def to_row(self) -> dict[str, Any]:
        return {
            "ext_id": self.ext_id,
            "title": self.title,
            "company": self.company,
            "salary": self.salary_text,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "city": self.city,
            "district": self.district,
            "tags": self.tags,
            "url": self.url,
            "jd_text": self.jd_text,
        }


@dataclass
class MatchResult:
    passed: bool
    stage: str            # hard / llm / fallback
    score: int
    reason: str
    matched: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    pitch: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "match_score": self.score,
            "match_reason": self.reason,
            "match_pitch": self.pitch,
        }


# ---------------- 硬筛 ----------------

_EXP_RE = re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*年")


def _experience_requirement(tags: list[str]) -> int | None:
    """从标签里解析岗位要求的起始年限。

    返回 None 表示"不限/应届/无法判断"，不参与过滤。
    """
    for t in tags or []:
        t = str(t)
        if "不限" in t or "应届" in t or "在校" in t or "无经验" in t:
            return None
        if "1年以内" in t:
            return 0
        m = _EXP_RE.search(t)
        if m:
            return int(m.group(1))
        if "10年以上" in t:
            return 10
    return None

def hard_filter(job: JobInfo, profile: Profile) -> tuple[bool, str]:
    """按城市、薪资、职位名、否决词做第一道过滤。"""
    blob = f"{job.title} {job.company} {job.jd_text} {' '.join(job.tags)}"

    # 一票否决
    for word in profile.deal_breakers:
        if word and word in blob:
            return False, f"命中否决词「{word}」"

    # 城市
    cities = profile.target_cities
    if cities and job.city:
        if not any(c in job.city or job.city in c for c in cities):
            return False, f"城市不符（岗位：{job.city}）"

    # 职位名（大小写不敏感 —— "java程序员" 因为小写被误判过方向不符）
    titles = profile.target_titles
    if titles:
        title_low = (job.title or "").lower()
        if not any(t and (t.lower() in title_low) for t in titles):
            return False, f"职位方向不符（{job.title}）"

    # 经验层级：应届生投"5-10年"的岗位，既浪费额度也不礼貌。
    # 从标签里解析要求年限，允许"向上够一档"（0 年可以投 1-3 年）。
    req = _experience_requirement(job.tags)
    if req is not None:
        years = int(profile.years or 0)
        if req > years + 1:
            return False, f"经验要求不符（岗位要 {req} 年起，你 {years} 年）"

    # 薪资：用区间交集判断，不要求完全包含
    if profile.salary_min or profile.salary_max:
        lo = profile.salary_min or None
        hi = profile.salary_max or None
        if job.salary_min is not None or job.salary_max is not None:
            if not boss_font.in_range(
                {"min": job.salary_min, "max": job.salary_max}, lo, hi
            ):
                return False, (
                    f"薪资不符（岗位 {job.salary_text}，期望 {lo or '不限'}-{hi or '不限'}）"
                )

    return True, ""


# ---------------- 软筛 ----------------

PROMPT = """判断这个岗位是否适合这位求职者。

【求职者】
{profile}

【岗位】
{job}

【判断要求】
1. 技能匹配：候选人做得了这份工作吗？重点看核心职责是否在他的能力范围内。
2. 层级匹配：他的经验年限和这个岗位要求对等吗？严重过岗（大材小用）或欠格（撑不起来）都要扣分。
3. 硬伤识别：是否有单休、外包驻场、纯提成、明显不合理的加班等。

【输出 JSON，不要任何解释】
{{
  "score": 0-100 的整数,
  "matched": ["匹配点1", "匹配点2"],
  "gaps": ["短板1"],
  "pitch": "一句话，20字以内，必须引用 JD 或简历里的具体事实，可以直接放进招呼语"
}}

注意 pitch 的要求：
- 好例子："看到 JD 要短视频运营，我做过单条 500 万播放"
- 好例子："你们要做私域，我之前把公众号从 0 做到 20 万粉"
- 坏例子："我很适合这个岗位"（空话，没有信息量）
- 坏例子："希望能有机会聊聊"（同上）"""


def llm_match(job: JobInfo, profile: Profile) -> MatchResult:
    """模型打分。无 API Key 时自动降级为关键词规则。"""
    if not llm.available:
        return _rule_match(job, profile)

    user = PROMPT.format(profile=profile.summary(), job=job.as_jd())
    try:
        res = llm._chat("你是资深求职顾问，擅长判断岗位与候选人的真实匹配度。", user)
        data = llm._extract_json(res.text)
        if not data:
            return _rule_match(job, profile)

        score = int(data.get("score", 0) or 0)
        score = max(0, min(100, score))
        matched = [str(x) for x in (data.get("matched") or [])][:4]
        gaps = [str(x) for x in (data.get("gaps") or [])][:3]
        pitch = str(data.get("pitch", "") or "").strip()

        reason = "匹配：" + "；".join(matched) if matched else "模型未给出匹配点"
        if gaps:
            reason += "｜短板：" + "；".join(gaps)

        return MatchResult(
            passed=score >= _threshold(),
            stage="llm",
            score=score,
            reason=reason,
            matched=matched,
            gaps=gaps,
            pitch=pitch,
        )
    except Exception as exc:
        from ..db import log_event

        log_event("warn", "seeker", f"岗位匹配调用模型失败，降级规则引擎：{exc}")
        return _rule_match(job, profile)


def _threshold() -> int:
    return int((config.seeker or {}).get("run", {}).get("match_threshold", 65) or 65)


def _rule_match(job: JobInfo, profile: Profile) -> MatchResult:
    """无模型时的关键词打分。

    定位是"让链路能跑通"，不是替代模型。所以规则刻意简单：
    命中技能 +分数，命中否决词 -分数，最后按阈值判定。
    """
    blob = f"{job.title} {job.company} {job.jd_text} {' '.join(job.tags)}".lower()

    hits = [s for s in profile.skills if s and s.lower() in blob]
    extra = [h for h in profile.highlights if any(t.lower() in blob for t in h.tags)]

    # 打分权重按"真实招聘判断"调过：
    # 技能命中是最强信号（权重最高），标题方向次之，亮点补充，短板扣分。
    # 早期版本 base 45 + 命中 9 分/个，导致再匹配的岗位也到不了 65 的阈值。
    score = 42
    score += min(len(hits) * 10, 30)
    score += min(len(extra) * 6, 12)
    # 大小写不敏感：小写的 "java程序员" 之前匹配不到 "Java"，少加 8 分被卡在阈值下
    title_low = (job.title or "").lower()
    if any(t and t.lower() in title_low for t in profile.target_titles):
        score += 8

    gaps = profile.missing_skills(job.jd_text or blob)
    score -= min(len(gaps) * 4, 12)

    for word in profile.deal_breakers:
        if word and word in blob:
            score -= 30

    score = max(0, min(96, score))

    # pitch 用命中最相关的那条亮点。
    # 加"按职位 id 轮换"：如果所有岗位都用同一条亮点，一看就是模板群发，
    # 这正是我们要避免的。
    ranked = profile.highlights_for(blob)
    pitch = ""
    if ranked:
        idx = sum(ord(c) for c in (job.ext_id or "x")) % min(len(ranked), 3)
        pitch = ranked[idx].key
    elif hits:
        pitch = f"我做过{'、'.join(hits[:2])}"

    matched = hits[:3] + [h.key for h in extra[:2]]
    reason = ("命中：" + "、".join(matched)) if matched else "信息不足，按基础分处理"
    if gaps:
        reason += f"｜可能缺：{'、'.join(gaps[:3])}"

    return MatchResult(
        passed=score >= _threshold(),
        stage="fallback",
        score=score,
        reason=reason,
        matched=matched,
        gaps=gaps,
        pitch=pitch,
    )


def run_match(job: JobInfo, profile: Profile) -> MatchResult:
    """完整匹配流程：先硬筛，过了再软筛。"""
    ok, why = hard_filter(job, profile)
    if not ok:
        return MatchResult(passed=False, stage="hard", score=0, reason=why)
    return llm_match(job, profile)


# ---------------- 从文本解析（给 Mock 和采集用） ----------------

_SALARY_RE = re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*[Kk千]")


def parse_job_text(text: str, ext_id: str = "") -> JobInfo:
    """从一段职位卡片文本里粗略解析出结构化岗位信息。

    真实采集时优先从 DOM 各字段取值；这个函数是兜底，
    也用于 Mock 数据和纯文本输入。
    """
    text = text or ""
    info = JobInfo(ext_id=ext_id or text[:24])
    m = _SALARY_RE.search(text)
    if m:
        info.salary_min = int(m.group(1)) * 1000
        info.salary_max = int(m.group(2)) * 1000
        info.salary_text = f"{m.group(1)}-{m.group(2)}K"
    return info
