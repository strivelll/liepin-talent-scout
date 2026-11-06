"""浏览器驱动抽象层。

为什么要抽象：真实模式依赖 Patchright + 真实 Chrome + 客户账号，
在开发阶段和售前演示时这些都拿不到。把驱动抽出来之后：

- 业务逻辑（筛选、对话、判定）完全不知道自己跑在什么上面；
- MockDriver 可以在零账号、零网络的情况下跑通全链路，用于验证业务规则；
- 切到真实账号时只换驱动，不改一行业务代码。

这也是把"页面改版风险"关在一个文件里的做法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Card:
    """信息卡：列表页上能免费看到的信息，第一级硬筛只用这些。"""

    ext_id: str
    name: str = ""
    age: int | None = None
    education: str = ""
    experience: str = ""
    salary_expect: str = ""
    active_status: str = ""
    company: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ext_id": self.ext_id,
            "name": self.name,
            "age": self.age,
            "education": self.education,
            "experience": self.experience,
            "salary_expect": self.salary_expect,
            "active_status": self.active_status,
            "company": self.company,
            "tags": self.tags,
        }

    def as_text(self) -> str:
        """拼成自然语言，喂给模型做软筛。"""
        parts = [
            f"姓名：{self.name}" if self.name else "",
            f"年龄：{self.age}" if self.age else "",
            f"学历：{self.education}" if self.education else "",
            f"工作经验：{self.experience}" if self.experience else "",
            f"期望薪资：{self.salary_expect}" if self.salary_expect else "",
            f"当前公司：{self.company}" if self.company else "",
            f"活跃状态：{self.active_status}" if self.active_status else "",
            f"标签：{'、'.join(self.tags)}" if self.tags else "",
        ]
        return "\n".join(p for p in parts if p)


@dataclass
class IncomingMessage:
    """从平台读到的一条对方消息。"""

    text: str
    ext_id: str = ""


class BrowserDriver(ABC):
    """驱动接口。所有方法都是幂等的读或单次写，调度器负责编排。"""

    name: str = "base"

    @abstractmethod
    def open(self) -> None:
        """建立会话（真实模式加载登录态；mock 模式无操作）。"""

    @abstractmethod
    def close(self) -> None:
        """释放资源。"""

    @abstractmethod
    def fetch_candidates(self, job_id: str, limit: int = 20) -> list[Card]:
        """读取候选人列表，返回信息卡。"""

    @abstractmethod
    def greet(self, card: Card, text: str) -> bool:
        """发送打招呼消息。返回是否成功。"""

    @abstractmethod
    def fetch_unread(self) -> list[str]:
        """返回有未读消息的会话 ext_id 列表。"""

    @abstractmethod
    def read_messages(self, ext_id: str, limit: int = 30) -> list[IncomingMessage]:
        """读取某个会话的对方消息。"""

    @abstractmethod
    def send_message(self, ext_id: str, text: str) -> bool:
        """在会话里发送一条消息。"""

    @abstractmethod
    def risk_signal(self) -> str | None:
        """检测风控信号。返回 None 表示正常，否则返回信号类型
        （captcha / rate_limit / login_expired / daily_limit）。"""

    def screenshot(self, tag: str) -> str | None:
        """异常截图留证。默认不支持，真实驱动覆盖。"""
        return None
