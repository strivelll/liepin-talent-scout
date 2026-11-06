"""驱动工厂：按配置返回 Mock 或真实驱动。

Mock 保持单例，这样多次请求之间候选人会话状态是连续的（模拟真实平台的会话记忆）。
真实驱动每次新建，避免浏览器句柄被跨线程复用。
"""

from __future__ import annotations

import threading

from ..config import config
from .base import BrowserDriver, Card, IncomingMessage
from .mock import MockDriver

_MOCK: MockDriver | None = None
_LOCK = threading.Lock()

__all__ = ["BrowserDriver", "Card", "IncomingMessage", "MockDriver", "get_driver", "get_mock"]


def get_mock() -> MockDriver:
    """取 Mock 单例（演示与测试用，便于 reset / inject_risk）。"""
    global _MOCK
    with _LOCK:
        if _MOCK is None:
            _MOCK = MockDriver()
        return _MOCK


def get_driver() -> BrowserDriver:
    """按 settings.yaml 的 mode 返回驱动实例。"""
    if config.mode == "real":
        from .real import RealDriver

        return RealDriver()
    return get_mock()
