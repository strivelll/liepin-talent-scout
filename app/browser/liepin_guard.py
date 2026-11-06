"""猎聘操作的风控护栏。

**为什么单独抽一个模块**

2026-11-04 上午，遍历 30 个会话（每个点开等 3 秒）连续操作 90 多秒后，
账号被判「行为异常」，跳到 captchaPage_PC 要求人工验证。10 条消息的成果差点白费。

复盘下来问题不在"做了什么"，而在"节奏"：
- 固定 3 秒间隔 → 机器特征明显
- 30 次连续点击 → 单次会话内动作密度过高
- 只在最后才检查 URL → 撞上验证码才知道

所以把三件事固化成护栏：
1. 随机节奏（不是固定值）
2. 单次批量上限
3. 每 N 步主动体检
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

# 实测触发的阈值参考：
# 30 次点击 / 90 秒 → 触发。保守取值留一倍余量。
MAX_ACTIONS_PER_RUN = 12       # 单次运行最多动作数
CHECK_EVERY = 3                # 每 N 个动作检查一次风控
ACTION_DELAY = (3.5, 8.0)      # 动作间隔（秒），随机
PAGE_DELAY = (4.0, 7.0)        # 页面跳转后等待

RISK_URL_MARKS = ("captcha", "safe.liepin", "verify", "login")
RISK_TEXT_MARKS = ("行为异常", "操作过于频繁", "请完成验证", "安全验证", "账号存在异常")


class RiskGuard:
    """风控护栏。所有页面动作都该经过它。

    用法：
        guard = RiskGuard(page)
        for item in items:
            guard.before_action()          # 会等随机间隔
            do_something(item)
            if guard.check():              # 返回 True 表示触发风控
                break
    """

    def __init__(self, page: Any, max_actions: int = MAX_ACTIONS_PER_RUN) -> None:
        self.page = page
        self.max_actions = max_actions
        self.count = 0
        self.tripped: str | None = None
        self._since_check = 0

    # ---------- 体检 ----------

    def check(self) -> str | None:
        """检测是否触发风控。命中则记录并返回原因。"""
        try:
            url = self.page.url or ""
        except Exception:
            return None
        for m in RISK_URL_MARKS:
            if m in url:
                self.tripped = f"url:{m}"
                return self.tripped
        try:
            body = self.page.inner_text("body")[:3000]
        except Exception:
            return None
        for m in RISK_TEXT_MARKS:
            if m in body:
                self.tripped = f"text:{m}"
                return self.tripped
        return None

    # ---------- 节奏 ----------

    def before_action(self, label: str = "") -> bool:
        """动作前的等待与配额检查。返回 False 表示应停止。"""
        if self.tripped:
            return False
        if self.count >= self.max_actions:
            self.tripped = "quota"
            return False

        # 每 CHECK_EVERY 步主动体检一次，不要等撞上验证码
        if self._since_check >= CHECK_EVERY:
            if self.check():
                return False
            self._since_check = 0

        delay = random.uniform(*ACTION_DELAY)
        if label:
            print(f"      [{label}] 等待 {delay:.1f}s")
        time.sleep(delay)
        return True

    def after_action(self) -> None:
        self.count += 1
        self._since_check += 1

    def after_nav(self) -> None:
        """页面跳转后等待渲染。"""
        time.sleep(random.uniform(*PAGE_DELAY))

    # ---------- 报告 ----------

    def summary(self) -> str:
        if self.tripped == "quota":
            return f"达到单次上限 {self.max_actions} 次，正常停止"
        if self.tripped:
            return f"⚠ 触发风控（{self.tripped}），已停止，需人工验证后 24 小时再试"
        return f"完成 {self.count} 个动作，未触发风控"

    @property
    def blocked(self) -> bool:
        return self.tripped is not None


def safe_call(fn: Callable[[], Any], default: Any = None) -> Any:
    """包一层异常，避免单个元素异常打断整批。"""
    try:
        return fn()
    except Exception:
        return default


# ---------- 会话/聊天页的通用操作 ----------

def click_element(page: Any, selector: str, text: str | None = None) -> bool:
    """点击元素。

    **必须用真实鼠标坐标**：实测猎聘的会话项、按钮对 `el.click()` 和
    `el.evaluate("el => el.click()")` 都不响应（0 请求），
    只有 `page.mouse.click(x, y)` 能触发。

    返回是否点中。
    """
    els = page.query_selector_all(selector)
    for el in els:
        try:
            if text and text not in (el.inner_text() or ""):
                continue
            try:
                el.scroll_into_view_if_needed(timeout=2500)
            except Exception:
                pass
            time.sleep(0.4)
            box = el.bounding_box()
            if not box:
                continue
            # 必须落在视口内，否则 mouse.click 会点到别处
            if not (0 < box["y"] < page.viewport_size.get("height", 900) - 60):
                continue
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return True
        except Exception:
            continue
    return False
