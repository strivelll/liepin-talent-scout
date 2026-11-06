"""真实驱动：Patchright + 系统 Chrome。

几个关键决定的理由：

1. **用 Patchright 而不是原生 Playwright**：原生版在执行页面脚本时会触发 CDP 的
   `Runtime.enable`，这个动作在协议层留下可观测痕迹，是 2025 年后最主要的自动化
   检测信号，JS 层补丁修不了。Patchright 改写了这条调用路径。

2. **channel="chrome" 指向系统已装 Chrome**：Playwright 自带的 Chromium 有特征，
   而真实 Chrome 的 TLS 指纹本身就是最好的伪装。这一点比任何补丁都有效。

3. **不用无头模式**：headless 有一整套独立特征（渲染、字体、WebGL 都不同）。
   本系统定位是"一机一号、有人值守"，用有头模式完全可接受。

4. **选择器多候选 + 文案双保险**：页面改版时选择器先失效，但"今日沟通已达上限"
   这类文案相对稳定。两者同时判断，比单一手段可靠。
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import ROOT, config
from ..db import log_event
from .base import BrowserDriver, Card, IncomingMessage

try:
    from patchright.sync_api import sync_playwright  # type: ignore
except Exception:  # pragma: no cover
    sync_playwright = None  # type: ignore


class DriverUnavailable(RuntimeError):
    """真实驱动不可用（未安装 Patchright / 未登录 / Chrome 缺失）。"""


class RealDriver(BrowserDriver):
    """真实页面驱动。注意：本类为同步实现，必须跑在独立线程里。"""

    name = "real"

    def __init__(self) -> None:
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._sel = config.selectors
        self._beh = self._sel.get("behavior", {}) or {}
        self._profile_dir = ROOT / "data" / "chrome-profile"
        self._profile_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 生命周期 ----------

    def open(self) -> None:
        if sync_playwright is None:
            raise DriverUnavailable(
                "未安装 patchright。请执行：pip install patchright && patchright install chromium"
            )
        if self._ctx is not None:
            return
        self._pw = sync_playwright().start()
        # persistent context 保留登录态；channel=chrome 用系统真实浏览器
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            channel="chrome",
            headless=False,
            viewport=None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        log_event("info", "system", "浏览器已启动（真实模式，需人工登录一次）")

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            self._ctx = None
            self._page = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None

    @property
    def page(self) -> Any:
        if self._page is None:
            raise DriverUnavailable("浏览器未启动，请先调用 open()")
        return self._page

    # ---------- 元素定位 ----------

    def _candidates(self, path: str) -> list[str]:
        """按 'pages.chat.input_box' 这样的路径取候选选择器列表。"""
        node: Any = self._sel
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return []
            node = node[part]
        if isinstance(node, str):
            return [node]
        if isinstance(node, list):
            return [str(x) for x in node]
        return []

    def _find(self, scope: Any, path: str, timeout_ms: int = 2500) -> Any:
        """依次尝试候选选择器，返回第一个命中的元素。"""
        sels = self._candidates(path)
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            for sel in sels:
                try:
                    el = scope.query_selector(sel)
                    if el is not None:
                        return el
                except Exception:
                    continue
            time.sleep(0.2)
        return None

    def _find_all(self, scope: Any, path: str) -> list[Any]:
        for sel in self._candidates(path):
            try:
                items = scope.query_selector_all(sel)
                if items:
                    return list(items)
            except Exception:
                continue
        return []

    def _text(self, scope: Any, path: str, default: str = "") -> str:
        el = self._find(scope, path, timeout_ms=800)
        if el is None:
            return default
        try:
            return (el.inner_text() or "").strip()
        except Exception:
            return default

    # ---------- 拟人化动作 ----------

    def _human_pause(self, key: str = "read_dwell_sec") -> None:
        lo, hi = config.pacing.get(key, [1, 2])
        time.sleep(random.uniform(float(lo), float(hi)))

    def _human_click(self, el: Any) -> None:
        """在元素包围盒内随机偏移点击，避免恒定落在几何中心。"""
        try:
            box = el.bounding_box()
            if not box:
                el.click()
                return
            offset = int(self._beh.get("click_offset_px", 6))
            x = random.uniform(offset, max(offset + 1, box["width"] - offset))
            y = random.uniform(offset, max(offset + 1, box["height"] - offset))
            el.click(position={"x": x, "y": y})
        except Exception:
            try:
                el.click()
            except Exception as exc:
                log_event("warn", "system", f"点击失败：{exc}")
                raise

    def _human_type(self, el: Any, text: str) -> None:
        """逐字符输入，间隔随机；避免 paste 式的瞬时填充。"""
        lo, hi = self._beh.get("typing_delay_ms", [40, 140])
        el.click()
        for ch in text:
            el.type(ch, delay=0)
            time.sleep(random.uniform(float(lo), float(hi)) / 1000)

    # ---------- 风控识别 ----------

    def risk_signal(self) -> str | None:
        """选择器 + 文案双重判断。先看文案，因为它更稳。"""
        try:
            body = self.page.inner_text("body") or ""
        except Exception:
            return None
        texts = self._sel.get("pages", {}).get("risk_signals", {}).get("texts", {}) or {}

        for kw in texts.get("login_expired", []) or []:
            if kw and kw in body:
                return "login_expired"
        for kw in texts.get("daily_limit", []) or []:
            if kw and kw in body:
                return "daily_limit"
        for kw in texts.get("risk_warning", []) or []:
            if kw and kw in body:
                return "rate_limit"

        if self._find(self.page, "pages.risk_signals.captcha", timeout_ms=300):
            return "captcha"
        if self._find(self.page, "pages.risk_signals.rate_limit", timeout_ms=300):
            return "rate_limit"
        return None

    def screenshot(self, tag: str) -> str | None:
        """异常截图，既是留证也是调参依据。"""
        try:
            folder = config.path("screenshot_dir", "data/screenshots")
            if folder.suffix:
                folder = folder.parent
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            path = folder / name
            self.page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:
            return None

    # ---------- 接口实现 ----------

    def fetch_candidates(self, job_id: str, limit: int = 20) -> list[Card]:
        """从列表页读信息卡。只读可见字段，不点进详情（零成本）。"""
        page = self.page
        if not self._find(page, "pages.recommend_list.list_container", timeout_ms=8000):
            log_event("warn", "system", "未找到候选人列表容器，可能页面未打开或选择器需更新")
            return []

        items = self._find_all(page, "pages.recommend_list.item")[:limit]
        cards: list[Card] = []
        fields = self._sel.get("pages", {}).get("recommend_list", {}).get("fields", {}) or {}

        for idx, item in enumerate(items):
            def grab(field: str) -> str:
                path = fields.get(field)
                if not path:
                    return ""
                cands = path if isinstance(path, list) else [path]
                for sel in cands:
                    try:
                        el = item.query_selector(sel)
                        if el is not None:
                            return (el.inner_text() or "").strip()
                    except Exception:
                        continue
                return ""

            name = grab("name")
            if not name:
                continue
            tags_el = []
            tag_path = fields.get("tags") or []
            for sel in (tag_path if isinstance(tag_path, list) else [tag_path]):
                try:
                    tags_el = item.query_selector_all(sel)
                    if tags_el:
                        break
                except Exception:
                    continue
            tags = []
            for t in tags_el:
                try:
                    txt = (t.inner_text() or "").strip()
                    if txt:
                        tags.append(txt)
                except Exception:
                    continue

            age_txt = grab("age")
            age = None
            digits = "".join(ch for ch in age_txt if ch.isdigit())
            if digits:
                age = int(digits[:2])

            cards.append(
                Card(
                    ext_id=f"{job_id}-{idx}-{name}",
                    name=name,
                    age=age,
                    education=grab("education"),
                    experience=grab("experience"),
                    salary_expect=grab("salary"),
                    active_status=grab("active_status"),
                    company=grab("company"),
                    tags=tags,
                )
            )
            self._human_pause("read_dwell_sec")
        return cards

    def greet(self, card: Card, text: str) -> bool:
        """点击打招呼按钮，输入话术后发送。"""
        page = self.page
        items = self._find_all(page, "pages.recommend_list.item")
        for item in items:
            try:
                name_el = None
                for sel in self._candidates("pages.recommend_list.fields.name"):
                    name_el = item.query_selector(sel)
                    if name_el:
                        break
                if name_el is None or card.name not in (name_el.inner_text() or ""):
                    continue

                btn = None
                for sel in self._candidates("pages.recommend_list.greet_button"):
                    btn = item.query_selector(sel)
                    if btn:
                        break
                if btn is None:
                    return False

                self._human_click(btn)
                self._human_pause("read_dwell_sec")

                box = self._find(page, "pages.chat.input_box", timeout_ms=3000)
                if box is None:
                    return False
                self._human_type(box, text)
                self._human_pause()
                send = self._find(page, "pages.chat.send_button", timeout_ms=2000)
                if send is not None:
                    self._human_click(send)
                else:
                    box.press("Enter")
                self._human_pause()
                return True
            except Exception as exc:
                log_event("warn", "greet", f"打招呼异常：{exc}", None)
                continue
        return False

    def fetch_unread(self) -> list[str]:
        """只返回有未读标记的会话，避免空转消耗。"""
        page = self.page
        unread: list[str] = []
        sessions = self._find_all(page, "pages.chat.session_item")
        for s in sessions:
            try:
                badge = None
                for sel in self._candidates("pages.chat.unread_badge"):
                    badge = s.query_selector(sel)
                    if badge:
                        break
                if badge is None:
                    continue
                name_el = None
                for sel in self._candidates("pages.chat.session_name"):
                    name_el = s.query_selector(sel)
                    if name_el:
                        break
                if name_el is None:
                    continue
                unread.append((name_el.inner_text() or "").strip())
            except Exception:
                continue
        return [u for u in unread if u]

    def read_messages(self, ext_id: str, limit: int = 30) -> list[IncomingMessage]:
        page = self.page
        msgs: list[IncomingMessage] = []
        for item in self._find_all(page, "pages.chat.message_item")[-limit:]:
            try:
                is_them = False
                for sel in self._candidates("pages.chat.msg_from_them"):
                    if item.query_selector(sel):
                        is_them = True
                        break
                if not is_them:
                    continue
                text = ""
                for sel in self._candidates("pages.chat.msg_text"):
                    el = item.query_selector(sel)
                    if el is not None:
                        text = (el.inner_text() or "").strip()
                        break
                if text:
                    msgs.append(IncomingMessage(text=text, ext_id=ext_id))
            except Exception:
                continue
        return msgs

    def send_message(self, ext_id: str, text: str) -> bool:
        page = self.page
        box = self._find(page, "pages.chat.input_box", timeout_ms=4000)
        if box is None:
            return False
        try:
            self._human_type(box, text)
            self._human_pause()
            send = self._find(page, "pages.chat.send_button", timeout_ms=2000)
            if send is not None:
                self._human_click(send)
            else:
                box.press("Enter")
            return True
        except Exception as exc:
            log_event("warn", "chat", f"发送失败：{exc}")
            return False
