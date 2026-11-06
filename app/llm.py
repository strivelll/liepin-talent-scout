"""大模型调用层。

三个设计要点：

1. **可切换 provider**：走 OpenAI 兼容协议，DeepSeek / OpenAI / 自建网关都能用，
   换模型只改 settings.yaml，不动代码。

2. **无 Key 也能跑**：未配置 API Key 时自动降级为规则引擎（RuleFallback）。
   这不是占位实现——它用关键词与打分规则模拟真实判定，让 MVP 在没有模型额度时
   也能完整演示链路。接上 Key 后无缝切换到真实模型。

3. **成本可观测**：每次调用记录 token 与估算费用，落到 daily_stats。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .config import config
from .db import add_llm_cost, log_event

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover - 依赖缺失时降级
    OpenAI = None  # type: ignore


@dataclass
class LLMResult:
    text: str
    tokens: int = 0
    cost: float = 0.0
    provider: str = "rule"


class RuleFallback:
    """无模型时的规则引擎。

    只覆盖 MVP 需要的三类判断：岗位匹配打分、对话回复、合格判定。
    规则刻意做得简单，因为它的定位是"让链路能跑通"，而不是替代模型。
    """

    POSITIVE = ["销售", "客户", "业绩", "提成", "面销", "电销", "b端", "企业", "saas", "软件"]
    NEGATIVE = ["实习", "兼职", "在校", "应届"]
    SENIOR = ["主管", "经理", "总监", "负责人", "top", "销冠"]
    PATENT = ["专利", "撰写", "oa", "答复", "审查意见", "代理人", "知识产权"]

    def score(self, text: str) -> tuple[int, str]:
        low = (text or "").lower()
        score = 45
        hits = [w for w in self.POSITIVE + self.PATENT if w in low]
        score += min(len(hits) * 8, 32)
        if any(w in low for w in self.SENIOR):
            score += 10
        if any(w in low for w in self.NEGATIVE):
            score -= 25
        score = max(0, min(100, score))
        reason = f"命中关键词：{'、'.join(hits[:4])}" if hits else "信息不足，按基础分处理"
        return score, reason

    def reply(self, persona: str, history: list[dict[str, str]], question: str) -> str:
        """按剧本抛出下一个问题，语气做轻量拟人化。"""
        last = ""
        for m in reversed(history):
            if m.get("role") == "them":
                last = m.get("content", "")
                break
        prefix = "明白，" if last else ""
        return f"{prefix}那{question}"

    def judge(self, them_text: str, criteria: str) -> tuple[int, str]:
        """按"信息充分度 + 正向信号 - 负向信号"打分。

        注意入参只应包含**候选人自己说的话**。如果把招聘方的提问也算进来，
        "什么时候能到岗"这类问句会被误判成候选人已经答复了到岗时间。
        """
        low = (them_text or "").lower()
        base = 45
        pos = 0
        hits: list[str] = []

        # 经验年限
        if any(w in low for w in ["一年", "两年", "三年", "四年", "五年", "多年",
                                  "1年", "2年", "3年", "4年", "5年"]):
            pos += 10
            hits.append("有年限说明")
        # 客户类型（B 端信号）
        if any(w in low for w in ["企业", "b端", "客户", "项目", "大客户", "国企", "上市公司"]):
            pos += 12
            hits.append("B端经验")
        # 量化业绩
        if any(w in low for w in ["万", "单", "件", "业绩", "完成", "超额", "续约", "top", "第二", "销冠"]):
            pos += 10
            hits.append("有量化业绩")
        # 专业动作（专利场景）
        if any(w in low for w in ["oa", "答复", "复审", "撰写", "无效", "fto", "审查意见"]):
            pos += 12
            hits.append("具备专业动作")
        # 到岗可行性
        if any(w in low for w in ["随时", "一周", "两周", "一个月", "已离职", "离职", "到岗"]):
            pos += 8
            hits.append("到岗时间明确")

        neg = 0
        if any(w in low for w in ["不考虑", "太远", "兼职", "只看远程", "不出差"]):
            neg += 30
            hits.append("存在硬性冲突")
        if "应届" in low or "实习" in low:
            neg += 20
            hits.append("经验不足")

        score = max(0, min(96, base + pos - neg))
        reason = "命中：" + "、".join(hits) if hits else "信息不足，按基础分处理"
        return score, reason


class LLMClient:
    """统一入口：真实模型优先，无 Key 时降级到规则引擎。"""

    def __init__(self) -> None:
        self._client: Any = None
        self._fallback = RuleFallback()
        self._init_client()

    def _init_client(self) -> None:
        cfg = config.llm_cfg
        key = config.api_key()
        if not cfg.get("enabled", True) or not key or OpenAI is None:
            self._client = None
            return
        try:
            self._client = OpenAI(
                api_key=key,
                base_url=str(cfg.get("base_url") or "https://api.deepseek.com/v1"),
                timeout=float(cfg.get("timeout", 30)),
            )
        except Exception as exc:  # pragma: no cover
            log_event("warn", "system", f"模型客户端初始化失败，降级为规则引擎：{exc}")
            self._client = None

    def reload(self) -> None:
        """配置或环境变量变化后重建客户端。"""
        self._init_client()

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def provider_name(self) -> str:
        if self._client is None:
            return "rule"
        return str(config.llm_cfg.get("provider", "openai"))

    # ---------- 底层调用 ----------

    def _chat(self, system: str, user: str) -> LLMResult:
        cfg = config.llm_cfg
        resp = self._client.chat.completions.create(
            model=str(cfg.get("model", "deepseek-chat")),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=float(cfg.get("temperature", 0.7)),
            max_tokens=int(cfg.get("max_tokens", 500)),
        )
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        cost = (
            prompt_tokens / 1000 * float(cfg.get("price_per_1k_input", 0.001))
            + completion_tokens / 1000 * float(cfg.get("price_per_1k_output", 0.002))
        )
        add_llm_cost(prompt_tokens + completion_tokens, cost)
        return LLMResult(text=text, tokens=prompt_tokens + completion_tokens, cost=cost,
                         provider=self.provider_name)

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """从模型输出里稳健地抠出 JSON（兼容 ```json 包裹、前后废话）。"""
        if not text:
            return {}
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if fenced:
            text = fenced.group(1)
        else:
            brace = re.search(r"\{.*\}", text, re.S)
            if brace:
                text = brace.group(0)
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ---------- 业务接口 ----------

    def score_candidate(self, job_name: str, prompt: str, card_text: str) -> tuple[int, str]:
        """简历/信息卡匹配打分，返回 (分数, 理由)。"""
        if self._client is None:
            return self._fallback.score(card_text)
        system = (
            "你是招聘筛选助手。严格按照要求的 JSON 格式输出，不要输出任何解释性文字。"
        )
        user = f"岗位：{job_name}\n筛选要求：{prompt}\n\n候选人信息：\n{card_text}"
        try:
            res = self._chat(system, user)
            data = self._extract_json(res.text)
            score = int(data.get("score", 0) or 0)
            reason = str(data.get("reason", "") or "")
            if not (0 <= score <= 100):
                score = max(0, min(100, score))
            return score, reason or "模型未给出理由"
        except Exception as exc:
            log_event("warn", "system", f"模型打分失败，降级规则引擎：{exc}")
            return self._fallback.score(card_text)

    def generate_reply(
        self,
        persona: str,
        company_intro: str,
        history: list[dict[str, str]],
        question: str,
        candidate_name: str = "",
    ) -> str:
        """生成一句自然回复，并把当前该问的问题融进去。"""
        if self._client is None:
            return self._fallback.reply(persona, history, question)
        system = f"{persona}\n\n公司背景：{company_intro}\n\n注意：你正在 BOSS 直聘上聊天，回复要短、口语化、像真人手机打字。"
        lines = []
        for m in history[-int(config.chat_cfg.get("context_window", 20)):]:
            who = "候选人" if m.get("role") == "them" else "我"
            lines.append(f"{who}：{m.get('content', '')}")
        transcript = "\n".join(lines) or "（暂无历史）"
        user = (
            f"候选人：{candidate_name or '对方'}\n\n对话记录：\n{transcript}\n\n"
            f"这一轮你需要自然地问到：{question}\n"
            "要求：不要生硬照搬这句话，要结合上下文改写成口语；只输出你要发的那一条消息，不要加引号或说明。"
        )
        try:
            res = self._chat(system, user)
            text = res.text.strip().strip('"').strip("“”")
            return text or self._fallback.reply(persona, history, question)
        except Exception as exc:
            log_event("warn", "system", f"模型生成回复失败，降级规则引擎：{exc}")
            return self._fallback.reply(persona, history, question)

    @staticmethod
    def _them_only(transcript: str) -> str:
        """从带说话人的对话文本里，只抽出候选人说的话。

        规则引擎做关键词打分时不能看招聘方的提问，否则会把"什么时候能到岗"
        这种问句当成候选人已经答复了到岗时间。
        """
        out = []
        for line in (transcript or "").split("\n"):
            if line.startswith("候选人："):
                out.append(line[len("候选人："):])
        return "\n".join(out)

    def judge_candidate(
        self, criteria: str, transcript: str, threshold: int = 70
    ) -> tuple[int, str, str]:
        """综合判定，返回 (分数, 结论, 理由)。

        threshold 由岗位配置传入。早期版本在底层写死 70，导致岗位阈值设 65 时
        67 分的合格销售仍被判掉——阈值只能有一处定义，就是岗位配置。
        """
        if self._client is None:
            score, reason = self._fallback.judge(self._them_only(transcript), criteria)
            return score, ("qualified" if score >= threshold else "rejected"), reason
        system = "你是招聘官，负责根据对话记录判断候选人是否合格。严格输出 JSON，不要解释。"
        user = (
            f"判定标准：\n{criteria}\n\n完整对话记录：\n{transcript}\n\n"
            '输出 JSON：{"score": 0-100, "verdict": "qualified 或 rejected", "reason": "一句话理由"}'
        )
        try:
            res = self._chat(system, user)
            data = self._extract_json(res.text)
            score = int(data.get("score", 0) or 0)
            verdict = str(data.get("verdict", "") or "").strip()
            reason = str(data.get("reason", "") or "模型未给出理由")
            score = max(0, min(100, score))
            # 分子是模型的语义判断，但分数低于岗位阈值时以分数为准
            if verdict not in ("qualified", "rejected"):
                verdict = "qualified" if score >= threshold else "rejected"
            if verdict == "qualified" and score < threshold:
                verdict = "rejected"
            return score, verdict, reason
        except Exception as exc:
            log_event("warn", "system", f"模型判定失败，降级规则引擎：{exc}")
            score, reason = self._fallback.judge(self._them_only(transcript), criteria)
            return score, ("qualified" if score >= threshold else "rejected"), reason

    def classify_intent(self, text: str) -> str:
        """轻量意图识别：明确拒绝 / 提问 / 正常回答。

        这一步特意不调模型——它在高频路径上，规则足够且零成本。
        这也符合"模型负责想、脚本负责做"的分工。
        """
        low = (text or "").strip().lower()
        if not low:
            return "empty"
        reject_words = ["不考虑", "不合适", "没兴趣", "不感兴趣", "暂时不看", "不看了", "算了", "不用了"]
        if any(w in low for w in reject_words):
            return "reject"
        handover_words = [str(w).lower() for w in config.chat_cfg.get("handover_keywords", [])]
        if any(w in low for w in handover_words):
            return "handover"
        if low.endswith("?") or low.endswith("？") or "吗" in low or "多少" in low:
            return "question"
        return "answer"


llm = LLMClient()
