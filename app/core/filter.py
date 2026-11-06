"""三级漏斗筛选。

这是整个系统省钱的关键。如果对每个候选人都点开看完整简历再判断，
平台点数消耗会远超开发费用——很多同类项目上线后"越用越贵"就是这么来的。

三级设计：
  第一级 硬筛：只读列表页信息卡（年龄/学历/年限/活跃度/薪资/排除词），零成本
  第二级 软筛：把信息卡交给模型打分，成本约 0.01 元/人
  第三级 精读：只对高分者点开完整简历，消耗平台点数，按日限量

同样的筛选精度，第三级调用量能压到原来的十分之一量级。
"""

from __future__ import annotations

import re
from typing import Any

from ..browser.base import Card
from ..config import JobConfig
from ..llm import llm


def parse_experience(text: str) -> int:
    """把 '3-5年' / '10年以上' / '应届生' 解析成起始年限。"""
    if not text:
        return 0
    t = text.strip()
    if any(k in t for k in ("应届", "在校", "实习", "无经验")):
        return 0
    m = re.search(r"(\d+)", t)
    if m:
        return int(m.group(1))
    return 0


def parse_salary_upper(text: str) -> int | None:
    """把 '10-15K' / '20K以上' / '面议' 解析成月薪上限（元）。"""
    if not text:
        return None
    t = text.replace(" ", "")
    if "面议" in t:
        return None
    nums = [int(x) for x in re.findall(r"(\d+)", t)]
    if not nums:
        return None
    val = max(nums)
    # K / 千 单位换算
    if "k" in t.lower() or "千" in t:
        return val * 1000
    if "万" in t:
        return val * 10000
    # 纯数字：小于 100 视为 K
    return val * 1000 if val < 100 else val


def parse_active_days(text: str) -> int:
    """把活跃状态文案解析成"最近几天活跃"。数值越小越活跃。"""
    if not text:
        return 99
    t = text.strip()
    if "刚刚" in t or "在线" in t:
        return 0
    if "今日" in t or "今天" in t:
        return 0
    if "昨日" in t or "昨天" in t:
        return 1
    m = re.search(r"(\d+)\s*日", t)
    if m:
        return int(m.group(1))
    if "本周" in t or "一周内" in t:
        return 7
    if "本月" in t or "两周内" in t:
        return 14
    if "月内" in t:
        return 30
    return 99


def hard_filter(card: Card, job: JobConfig) -> tuple[bool, str]:
    """第一级：信息卡硬筛。返回 (是否通过, 未通过原因)。"""
    rules = job.hard_filter or {}
    if not rules:
        return True, ""

    # 年龄
    age_rule = rules.get("age") or {}
    if card.age and age_rule:
        lo = int(age_rule.get("min", 0))
        hi = int(age_rule.get("max", 99))
        if not (lo <= card.age <= hi):
            return False, f"年龄 {card.age} 不在 {lo}-{hi} 区间"

    # 学历（命中其一即通过；候选人学历文案包含配置项也算命中）
    edu_list = rules.get("education") or []
    if edu_list and card.education:
        if not any(str(e) in card.education for e in edu_list):
            return False, f"学历 {card.education} 不符合要求"

    # 经验年限
    exp_rule = rules.get("experience_years") or {}
    if exp_rule and card.experience:
        years = parse_experience(card.experience)
        lo = int(exp_rule.get("min", 0))
        hi = int(exp_rule.get("max", 99))
        if not (lo <= years <= hi):
            return False, f"经验 {card.experience}（按 {years} 年）不在 {lo}-{hi} 年"

    # 期望薪资上限
    sal_rule = rules.get("salary_expect") or {}
    if sal_rule and card.salary_expect:
        upper = parse_salary_upper(card.salary_expect)
        cap = sal_rule.get("max")
        if upper is not None and cap and upper > int(cap):
            return False, f"期望薪资 {card.salary_expect} 超出上限 {int(cap) // 1000}K"

    # 活跃度
    days_limit = rules.get("active_within_days")
    if days_limit and card.active_status:
        days = parse_active_days(card.active_status)
        if days > int(days_limit):
            return False, f"活跃度 {card.active_status} 超过 {days_limit} 天阈值"

    # 排除词（在姓名外的所有文本里找）
    excludes = rules.get("exclude_keywords") or []
    if excludes:
        blob = f"{card.education} {card.experience} {card.tags}"
        for kw in excludes:
            if kw and kw in blob:
                return False, f"命中排除词「{kw}」"

    # 必备标签
    required = rules.get("required_tags") or []
    if required:
        tag_blob = " ".join(card.tags) + " " + card.experience
        if not any(t in tag_blob for t in required):
            return False, f"缺少必备资质（需包含：{'、'.join(required)}）"

    return True, ""


def soft_screen(card: Card, job: JobConfig) -> tuple[int, str]:
    """第二级：模型软筛。成本约 0.01 元/人。"""
    cfg = job.soft_filter or {}
    threshold = int(cfg.get("threshold", 60))
    prompt = str(cfg.get("prompt", "")) or "判断该候选人是否与岗位基本匹配。"
    score, reason = llm.score_candidate(job.name, prompt, card.as_text())
    tag = "通过" if score >= threshold else "未达标"
    return score, f"{reason}（{score} 分，阈值 {threshold}，{tag}）"


def run_funnel(card: Card, job: JobConfig) -> dict[str, Any]:
    """跑完前两级，返回漏斗结论。

    第三级（精读）不在这里做——它需要打开详情页、消耗点数，
    由 pipeline 单独按日限额调度。
    """
    cfg_funnel = job.hard_filter

    if cfg_funnel and (config_hard_enabled()):
        ok, reason = hard_filter(card, job)
        if not ok:
            return {"stage": "hard", "passed": False, "score": 0, "reason": reason}

    if config_prescreen_enabled():
        score, reason = soft_screen(card, job)
        threshold = int((job.soft_filter or {}).get("threshold", 60))
        return {
            "stage": "soft",
            "passed": score >= threshold,
            "score": score,
            "reason": reason,
        }

    # 两级都关（理论上不该发生），视为通过，交给对话判断
    return {"stage": "none", "passed": True, "score": 0, "reason": "未启用筛选，直接进入对话"}


def config_hard_enabled() -> bool:
    from ..config import config

    return bool(config.funnel.get("hard_filter", True))


def config_prescreen_enabled() -> bool:
    from ..config import config

    return bool(config.funnel.get("llm_prescreen", True))
