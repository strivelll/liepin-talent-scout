"""Mock 驱动：不连 BOSS，用内置数据跑通全链路。

用途有三个：
1. 开发阶段验证业务逻辑（筛选规则、对话推进、判定、邀约）是否正确；
2. 给客户演示完整流程，不需要先交出账号；
3. 做回归测试——改完规则先跑 mock，再接真实账号。

设计要点：内置 12 个候选人，覆盖"硬筛淘汰 / 软筛低分 / 合格 / 问薪资转人工 /
明确拒绝 / 长时间不回"六种分支，保证链路的每条路径都能被走到。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any

from ..config import config
from .base import BrowserDriver, Card, IncomingMessage


def _cards() -> list[dict[str, Any]]:
    """内置候选人样本。回答按对话剧本顺序给，模拟真实推进。"""
    return [
        {
            "card": Card("m001", "张伟", 28, "本科", "3-5年", "10-15K", "刚刚活跃",
                         "某软件科技有限公司", ["企业服务", "B端销售", "SaaS"]),
            "answers": ["还在职，不过在看机会", "主要卖企业软件，都是 B 端客户，项目制那种",
                        "去年做了 300 多万，团队里第二", "期望 12k 左右，加提成",
                        "一个月内可以到岗"],
        },
        {
            "card": Card("m002", "李娜", 25, "大专", "1-3年", "8-10K", "今日活跃",
                         "某教育咨询公司", ["电话销售", "客户开发"]),
            "answers": ["已经离职了，随时能到", "之前做电销，卖课程，主要是个人客户",
                        "一个月大概二十多单吧，没细算", "8k 就行，主要看提成",
                        "随时可以"],
        },
        {
            "card": Card("m003", "王强", 34, "本科", "8-10年", "20-30K", "本周活跃",
                         "某信息技术公司", ["销售经理", "大客户", "团队管理"]),
            "answers": ["在职，考虑换个平台", "做企业信息化项目，客户是制造业",
                        "去年带团队做了 1200 万，个人 400 万", "期望不低于 25k，带团队",
                        "交接大概要一个月"],
        },
        {
            "card": Card("m004", "刘洋", 22, "大专", "应届生", "5-8K", "刚刚活跃",
                         "某大学", ["实习", "在校"]),
            "answers": ["还没毕业，想找实习"],
        },
        {
            "card": Card("m005", "陈静", 30, "硕士", "5-10年", "15-20K", "刚刚活跃",
                         "某云计算公司", ["SaaS", "大客户销售", "解决方案"]),
            "answers": ["在职看机会", "卖云服务和 SaaS，客户是中大型企业",
                        "去年 600 万左右，超额完成", "15k 底薪，加提成",
                        "两周左右"],
        },
        {
            "card": Card("m006", "赵磊", 26, "本科", "1-3年", "8-10K", "3日内活跃",
                         "某零售连锁", ["门店销售", "零售"]),
            "answers": ["在职", "在门店卖家电，面向个人顾客", "每个月十几万流水",
                        "8k 左右", "一个月"],
        },
        {
            "card": Card("m007", "孙婷", 29, "本科", "3-5年", "12-18K", "刚刚活跃",
                         "某企业咨询公司", ["企业服务", "咨询", "客户成功"]),
            "answers": ["在职，看机会", "做企业咨询项目，客户是国企和上市公司",
                        "去年负责 8 个客户，续约率 85%",
                        "想了解下你们薪资结构是怎么样的？底薪加提成比例多少？",
                        "一个月内"],
        },
        {
            "card": Card("m008", "周杰", 31, "本科", "5-10年", "15-20K", "今日活跃",
                         "某ERP厂商", ["ERP", "B端", "项目销售"]),
            "answers": ["在职", "卖 ERP，客户是制造业工厂", "去年 500 多万",
                        "15k 左右", "一个月内可以"],
        },
        {
            "card": Card("m009", "吴敏", 27, "本科", "3-5年", "12-16K", "刚刚活跃",
                         "某专利代理所", ["专利撰写", "电学", "OA答复"]),
            "answers": ["在职", "主要写电学和通信，一年大概 45 件",
                        "OA 都是自己答的，一年二十多件", "做过几次复审，FTO 没做过",
                        "12k 底薪加计件"],
        },
        {
            "card": Card("m010", "郑鹏", 33, "硕士", "8-10年", "20-28K", "本周活跃",
                         "某知识产权机构", ["专利代理人资格证", "通信", "复审无效"]),
            "answers": ["在职考虑", "通信和电子为主，一年 60 件左右",
                        "OA 全部自己做，复审也做", "做过复审无效，FTO 做过几个",
                        "期望 22k，希望有案源分成"],
        },
        {
            "card": Card("m011", "冯雪", 24, "本科", "1-3年", "6-8K", "3日内活跃",
                         "某专利代理所", ["流程管理", "商标"]),
            "answers": ["在职", "我做流程的，不太写案子，主要管时限和递交",
                        "商标也做一些", "没做过 OA", "6.5k"],
        },
        {
            "card": Card("m012", "何军", 36, "本科", "10年以上", "25-35K", "刚刚活跃",
                         "某大型代理机构", ["专利代理人资格证", "机械", "大客户"]),
            "answers": ["在职", "机械和新能源方向，一年 40 件", "OA 独立答复",
                        "复审做过不少", "底薪 25k 起"],
        },
    ]


class MockDriver(BrowserDriver):
    """内存态模拟驱动。"""

    name = "mock"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pool: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, list[IncomingMessage]] = {}
        self._answer_cursor: dict[str, int] = {}
        self._unread: set[str] = set()
        self._greeted: set[str] = set()
        self._sent: dict[str, list[str]] = {}
        self._risk: str | None = None
        self._build_pool()

    def _build_pool(self) -> None:
        for item in _cards():
            card: Card = item["card"]
            self._pool[card.ext_id] = {
                "card": card,
                "answers": list(item["answers"]),
            }
            self._sessions[card.ext_id] = []
            self._answer_cursor[card.ext_id] = 0
            self._sent[card.ext_id] = []

    # ---------- 生命周期 ----------

    def open(self) -> None:
        self._risk = None

    def close(self) -> None:
        pass

    # ---------- 列表与信息卡 ----------

    def fetch_candidates(self, job_id: str, limit: int = 20) -> list[Card]:
        """按岗位过滤候选池：销售岗取 m001-m008，专利岗取 m009-m012。"""
        with self._lock:
            if job_id.startswith("sales"):
                ids = [f"m{i:03d}" for i in range(1, 9)]
            elif "patent" in job_id:
                ids = [f"m{i:03d}" for i in range(9, 13)]
            else:
                ids = [f"m{i:03d}" for i in range(1, 9)]
            cards = [self._pool[i]["card"] for i in ids if i in self._pool]
            return cards[:limit]

    # ---------- 打招呼 ----------

    def greet(self, card: Card, text: str) -> bool:
        """模拟发送招呼。

        失败率做成可配置（默认 2%），而不是写死 10%。
        写死时演示会随机翻车——关键分支候选人（比如问薪资触发转人工的那位）
        恰好发送失败，整条链路就演示不出来。
        """
        with self._lock:
            if card.ext_id in self._greeted:
                return False
            fail_rate = float(config.settings.get("mock", {}).get("fail_rate", 0.02))
            if random.random() < fail_rate:
                return False
            self._greeted.add(card.ext_id)
            self._sent[card.ext_id].append(text)
            # 候选人回第一条消息（模拟对方看到招呼后回应）
            first = self._next_answer(card.ext_id)
            if first:
                self._sessions[card.ext_id].append(IncomingMessage(text=first, ext_id=card.ext_id))
                self._unread.add(card.ext_id)
            return True

    def _next_answer(self, ext_id: str) -> str | None:
        answers = self._pool.get(ext_id, {}).get("answers", [])
        idx = self._answer_cursor.get(ext_id, 0)
        if idx >= len(answers):
            return None
        self._answer_cursor[ext_id] = idx + 1
        return answers[idx]

    # ---------- 会话与消息 ----------

    def fetch_unread(self) -> list[str]:
        with self._lock:
            return sorted(self._unread)

    def read_messages(self, ext_id: str, limit: int = 30) -> list[IncomingMessage]:
        with self._lock:
            msgs = list(self._sessions.get(ext_id, []))
            self._unread.discard(ext_id)
            return msgs[-limit:]

    def send_message(self, ext_id: str, text: str) -> bool:
        with self._lock:
            if ext_id not in self._sessions:
                return False
            self._sent[ext_id].append(text)
            # 我方发出后，对方在"下一轮"再回一条，形成多轮对话
            nxt = self._next_answer(ext_id)
            if nxt:
                self._sessions[ext_id].append(IncomingMessage(text=nxt, ext_id=ext_id))
                self._unread.add(ext_id)
            return True

    # ---------- 风控 ----------

    def risk_signal(self) -> str | None:
        return self._risk

    # 测试钩子：让上层可以主动制造风控场景
    def inject_risk(self, signal: str | None) -> None:
        self._risk = signal

    def reset(self) -> None:
        """清空状态，便于重复演示。"""
        with self._lock:
            self._greeted.clear()
            self._unread.clear()
            for k in self._sessions:
                self._sessions[k].clear()
                self._sent[k].clear()
                self._answer_cursor[k] = 0
            self._risk = None
