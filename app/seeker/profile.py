"""求职者画像。

这个模块只做一件事：把 `config/seeker.yaml` 变成结构化对象，并提供
"给模型看的简历摘要"。所有匹配、话术、回复都从这里取素材。

设计要点：**画像里只放可验证的具体事实**。
"责任心强""沟通能力好"这类词对匹配和话术毫无价值，反而会让模型生成
空洞的自我介绍。所以 highlights 强制要求带量化结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import config


@dataclass
class Highlight:
    key: str
    tags: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class Profile:
    name: str = ""
    years: int = 0
    education: str = ""
    current: str = ""
    city: str = ""
    skills: list[str] = field(default_factory=list)
    highlights: list[Highlight] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)
    style: dict[str, Any] = field(default_factory=dict)
    resume: dict[str, Any] = field(default_factory=dict)

    # ---------- 从配置构建 ----------

    @classmethod
    def load(cls) -> "Profile":
        raw = (config.seeker or {}).get("profile", {}) or {}
        return cls(
            name=str(raw.get("name", "")),
            years=int(raw.get("years", 0) or 0),
            education=str(raw.get("education", "")),
            current=str(raw.get("current", "")),
            city=str(raw.get("city", "")),
            skills=[str(s) for s in (raw.get("skills") or [])],
            highlights=[
                Highlight(
                    key=str(h.get("key", "")),
                    tags=[str(t) for t in (h.get("tags") or [])],
                    detail=str(h.get("detail", "")),
                )
                for h in (raw.get("highlights") or [])
                if isinstance(h, dict)
            ],
            expected=raw.get("expected") or {},
            style=raw.get("style") or {},
            resume=raw.get("resume") or {},
        )

    # ---------- 派生信息 ----------

    @property
    def salary_min(self) -> int:
        return int(self.expected.get("salary_min", 0) or 0)

    @property
    def salary_max(self) -> int:
        return int(self.expected.get("salary_max", 0) or 0)

    @property
    def target_titles(self) -> list[str]:
        return [str(t) for t in (self.expected.get("titles") or [])]

    @property
    def target_cities(self) -> list[str]:
        return [str(c) for c in (self.expected.get("cities") or [])]

    @property
    def deal_breakers(self) -> list[str]:
        return [str(d) for d in (self.expected.get("deal_breakers") or [])]

    def summary(self) -> str:
        """给模型看的简历摘要。控制在 400 字内，太长会稀释信号。"""
        parts = [
            f"{self.name}，{self.education}，{self.years} 年经验",
            f"当前：{self.current}" if self.current else "",
            f"技能：{'、'.join(self.skills)}" if self.skills else "",
            "主要成果：",
        ]
        for h in self.highlights:
            parts.append(f"- {h.detail or h.key}")
        parts.append(
            f"期望：{'、'.join(self.target_cities)}｜{self.salary_min}-{self.salary_max} 元/月"
        )
        return "\n".join(p for p in parts if p)

    def highlights_for(self, text: str) -> list[Highlight]:
        """挑出与给定文本（通常是 JD）最相关的亮点。

        匹配方式：亮点标签或关键词是否出现在 JD 里。
        JD 里出现"短视频"就优先用短视频那条，而不是随便挑一条。
        """
        low = (text or "").lower()
        scored: list[tuple[int, Highlight]] = []
        for h in self.highlights:
            hit = 0
            for tag in h.tags:
                if tag and tag.lower() in low:
                    hit += 1
            for skill in self.skills:
                if skill and skill.lower() in low and skill in h.tags:
                    hit += 1
            scored.append((hit, h))
        scored.sort(key=lambda x: -x[0])
        return [h for _, h in scored]

    def missing_skills(self, text: str) -> list[str]:
        """JD 里提到、但画像里没有的技能类关键词（用于识别短板）。"""
        low = (text or "").lower()
        known = " ".join(self.skills + [h.key for h in self.highlights]).lower()
        # 只把**硬技术栈**算作短板。
        # "脚本""拍摄""活动策划"这类是运营岗的常规能力，算成短板会误伤——
        # 早期版本把它们计入扣分，导致所有运营岗被压到阈值以下。
        out: list[str] = []
        for kw in ("python", "sql", "tableau", "power bi", "ps", "pr", "ae",
                   "sem", "seo", "信息流投放", "直播运营", "私域运营"):
            if kw in low and kw not in known:
                out.append(kw)
        return out


def load_profile() -> Profile:
    """每次调用都重新读配置，方便用户改完 yaml 立刻生效。"""
    return Profile.load()
