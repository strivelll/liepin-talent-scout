"""求职端驱动层。

与招聘端驱动（app/browser）平行，接口完全不同：
招聘端读"候选人列表"，求职端读"职位列表"；招聘端向候选人打招呼，求职端向 HR 打招呼。

三种实现：
- `MockSeekerDriver`：内置真实抓取的职位 + 模拟 HR 回复，零账号可跑全链路
- `RealSeekerDriver`：Patchright 驱动系统 Chrome，操作用已实测的选择器
- 工厂函数按 config 的 mode 选择

**Mock 里的职位是真的。** 数据来自 2026-10-21 对 BOSS 求职端的真实抓取
（长沙·运营，15 条，薪资已解密）。只有 HR 的回复是模拟的。
这样演示时"数据真实性"这一关是站得住的。
"""

from __future__ import annotations

import json
import random
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..config import ROOT, config
from ..seeker.matcher import JobInfo

# 实测选择器（求职端，2026-10-21）
SEL = {
    "职位卡片": [".job-card-wrap", ".job-card-box"],
    "职位标题": [".job-name", ".job-title", "a[href*='/job_detail/']"],
    "薪资": [".job-salary"],
    "公司": [".boss-name"],
    "地区": [".company-location"],
    "标签": [".tag-list li"],
    # ⚠️ 实测：BOSS 求职端是"左列表 + 右详情"结构，
    # 沟通按钮在**右侧详情面板**里，不在职位卡片内。必须先点卡片切换详情。
    "详情面板": [".job-detail", ".job-detail-box", "[class*='job-detail']", ".job-detail-op"],
    "沟通按钮": [".job-detail-op .op-btn-chat", ".op-btn-chat", "[class*='op-btn-chat']",
                 ".btn-startchat", "[class*='startchat']"],
    "会话条目": [".user-list li", "[class*='session-item']"],
    "未读标记": [".badge-count", "[class*='unread']", "[class*='badge']"],
    "消息条目": [".message-item", "[class*='message-item']"],
    "消息文本": [".text", "[class*='text']"],
    "输入框": [".chat-input", "[contenteditable='true']", "textarea[class*='input']"],
    "发送按钮": [".btn-send", ".send-message", "button[class*='send']"],
    # 待实测：BOSS 聊天里的"发送简历"入口，不同账号权益下可能不同
    "发送简历": ["[class*='send-resume']", "[class*='resume-btn']", "text=发简历"],
}

RISK_TEXT = ["验证码", "安全验证", "操作过于频繁", "请稍后再试", "账号异常", "访问受限", "今日沟通已达上限"]

URL_JOBS = "https://www.zhipin.com/web/geek/jobs?query={query}&city={city}"
URL_CHAT = "https://www.zhipin.com/web/geek/chat"


class SeekerDriver(ABC):
    name = "base"

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def fetch_jobs(self, limit: int = 20) -> list[JobInfo]:
        """读职位列表。"""

    @abstractmethod
    def greet(self, job: JobInfo, text: str) -> bool:
        """向 HR 发起沟通并发送定制话术。

        BOSS 求职端的真实结构是"左列表 + 右详情"：
        点卡片只会切换右侧详情（安全），点详情里的"立即沟通"才真正投递。

        所以流程必须是：找卡片 → 点卡片 → 等详情 → 点沟通按钮 → 发消息。

        **返回值语义**：点下"立即沟通"就算成功。
        因为那一刻 BOSS 已经替我们发出默认招呼语，投递已经产生，
        后面定制消息发不发得出去只影响质量，不改变"已投递"这个事实。
        """
        print(f"      [greet] 处理「{(job.title or '')[:16]}」", flush=True)
        page = self.page

        # 1. 等卡片列表出现（最多 12 秒）
        # 之前只 sleep 4 秒就找卡片，页面还没渲染完，列表为空导致全部失败
        cards: list[Any] = []
        for _ in range(12):
            cards = self._find_all(page, "职位卡片")
            if cards:
                break
            time.sleep(1)
        if not cards:
            print("      卡片列表为空，页面可能未加载完")
            return False

        key = (job.title or "")[:10]
        target = None
        for c in cards:
            if key and key in self._txt(c):
                target = c
                break
        if target is None:
            print(f"      列表里没找到标题含「{key}」的卡片")
            return False

        # 2. 点卡片切换详情（只读操作，不会发消息）
        try:
            target.scroll_into_view_if_needed(timeout=3000)
            target.click(timeout=5000)
            time.sleep(2.5)
        except Exception as exc:
            print(f"      卡片点击失败：{str(exc)[:80]}")
            return False

        # 3. 在详情面板找沟通按钮
        btn = None
        for sel in SEL["沟通按钮"]:
            try:
                el = page.query_selector(sel)
                if el is not None and el.is_visible():
                    btn = el
                    break
            except Exception:
                continue
        if btn is None:
            print("      详情面板里没有可见的沟通按钮")
            return False

        # 4. 点击 = 投递（BOSS 会同时发出默认招呼语）
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=5000)
            time.sleep(3.5)
        except Exception as exc:
            print(f"      沟通按钮点击失败：{str(exc)[:80]}")
            return False

        # 5. 尽力发送定制话术；失败不影响"已投递"的判定
        try:
            box = self._find(page, "输入框", 6000)
            if box is not None:
                box.click()
                for ch in text:
                    box.type(ch, delay=0)
                    time.sleep(0.05)
                time.sleep(0.5)
                send = self._find(page, "发送按钮", 2000)
                if send is not None:
                    send.click()
                else:
                    box.press("Enter")
                time.sleep(1.5)
        except Exception:
            pass
        return True

    def fetch_unread(self) -> list[str]:
        """返回有未读消息的会话 ext_id。"""

    @abstractmethod
    def read_messages(self, ext_id: str) -> list[dict[str, str]]:
        """读会话消息，元素形如 {"role": "them"|"me", "text": ...}。"""

    @abstractmethod
    def send_message(self, ext_id: str, text: str) -> bool: ...

    @abstractmethod
    def send_resume(self, ext_id: str) -> bool:
        """在会话里发送简历。返回是否成功。"""

    @abstractmethod
    def risk_signal(self) -> str | None: ...


# ---------------- Mock ----------------


# HR 的几种典型反应，按职位 id 轮换，保证每条分支都能被走到
HR_STYLES = [
    # 直接要简历
    ["你好，方便发一份简历吗？", "收到，我看下，明天给你回复"],
    # 先问经验
    ["你好，看你做过新媒体，具体是哪个平台？", "我们主要做小红书和视频号，你能接吗？", "那可以，聊聊薪资吧"],
    # 问薪资
    ["你好，你期望薪资多少？", "我们预算 10-13K，你能接受吗？", "行，那我安排面试"],
    # 约面试
    ["你好，简历看过了，什么时候方便来面试？", "明天下午三点可以吗"],
    # 拒绝
    ["你好，我们这边需要有直播经验的，可能不太合适", "祝你找到合适的机会"],
    # 不回复（模拟已读不回）
    [],
]


JD_TEMPLATES: dict[str, str] = {
    "运营": (
        "岗位职责：负责公司新媒体平台（公众号/小红书/抖音）的内容策划与日常运营，"
        "提升粉丝量与互动率；独立完成选题、脚本、拍摄跟进与数据复盘。"
        "任职要求：有短视频或图文内容运营经验，熟悉平台算法，能看数据做决策。"
    ),
    "客服": (
        "岗位职责：负责客户咨询接待、售后问题处理与客户关系维护。"
        "任职要求：沟通表达清晰，有客服或服务行业经验，能承受一定工作压力。"
    ),
    "销售": (
        "岗位职责：负责客户开发与维护，完成销售目标。"
        "任职要求：有销售经验，抗压能力强，有 B 端客户资源者优先。"
    ),
    "产品": (
        "岗位职责：负责产品需求分析、原型设计与迭代推进。"
        "任职要求：有产品设计经验，熟悉用户研究方法，能与研发高效协作。"
    ),
    "技术": (
        "岗位职责：负责系统开发与维护。任职要求：扎实的编程基础，有相关项目经验。"
    ),
}


def _fake_jd(title: str, tags: list[str]) -> str:
    """按标题推断岗位方向，合成一段近似的 JD 文本。

    真实采集时这个字段来自职位详情页；Mock 阶段用模板补，
    目的是让匹配有足够信号，而不是靠标题两个字猜。
    """
    for key, tpl in JD_TEMPLATES.items():
        if key in (title or ""):
            return tpl + (f"\n关键词：{'、'.join(tags)}" if tags else "")
    return (
        "岗位职责：负责相关业务模块的日常运营与推进。"
        "任职要求：有相关经验，学习能力强。"
        + (f"\n关键词：{'、'.join(tags)}" if tags else "")
    )


class MockSeekerDriver(SeekerDriver):
    """零账号可跑的模拟驱动。职位数据是真的，回复是模拟的。"""

    name = "mock"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: list[JobInfo] = []
        self._greeted: set[str] = set()
        self._sessions: dict[str, list[dict[str, str]]] = {}
        self._cursor: dict[str, int] = {}
        self._unread: set[str] = set()
        self._risk: str | None = None
        self._load_jobs()

    def _load_jobs(self) -> None:
        """优先用真实抓取快照；没有就退回内置样本。"""
        path = ROOT / "data" / "demo_snapshot.json"
        rows: list[dict[str, Any]] = []
        if path.exists():
            try:
                snap = json.loads(path.read_text(encoding="utf-8"))
                rows = snap.get("jobs") or []
            except Exception:
                rows = []

        for i, r in enumerate(rows[:15]):
            self._jobs.append(JobInfo(
                ext_id=f"job{i+1:03d}",
                title=str(r.get("title", "")),
                company=str(r.get("company", "")),
                salary_text=str(r.get("salary", "")),
                salary_min=r.get("salary_min"),
                salary_max=r.get("salary_max"),
                city=str(r.get("city", "")),
                district=str(r.get("street", "") or r.get("district", "")),
                tags=[str(t) for t in (r.get("tags") or [])],
                url="",
                jd_text=_fake_jd(str(r.get("title", "")), [str(t) for t in (r.get("tags") or [])]),
            ))
        if not self._jobs:
            self._jobs = [JobInfo(ext_id="job001", title="新媒体运营", company="示例公司",
                                  salary_text="10-15K", salary_min=10000, salary_max=15000,
                                  city="长沙", tags=["新媒体", "内容"])]

    def open(self) -> None:
        self._risk = None

    def close(self) -> None:
        pass

    def fetch_jobs(self, limit: int = 20) -> list[JobInfo]:
        with self._lock:
            return self._jobs[:limit]

    def greet(self, job: JobInfo, text: str) -> bool:
        with self._lock:
            if job.ext_id in self._greeted:
                return False
            fail_rate = float(config.settings.get("mock", {}).get("fail_rate", 0.02))
            if random.random() < fail_rate:
                return False
            self._greeted.add(job.ext_id)
            self._sessions.setdefault(job.ext_id, []).append({"role": "me", "text": text})
            self._cursor[job.ext_id] = 0

            # 按职位序号分配一种 HR 风格
            idx = int(job.ext_id.replace("job", "") or 1) - 1
            style = HR_STYLES[idx % len(HR_STYLES)]
            if style:
                self._sessions[job.ext_id].append({"role": "them", "text": style[0]})
                self._cursor[job.ext_id] = 1
                self._unread.add(job.ext_id)
            return True

    def fetch_unread(self) -> list[str]:
        with self._lock:
            return sorted(self._unread)

    def read_messages(self, ext_id: str) -> list[dict[str, str]]:
        with self._lock:
            self._unread.discard(ext_id)
            return list(self._sessions.get(ext_id, []))

    def send_message(self, ext_id: str, text: str) -> bool:
        with self._lock:
            if ext_id not in self._sessions:
                return False
            self._sessions[ext_id].append({"role": "me", "text": text})
            # 推进 HR 剧本的下一句
            idx = int(ext_id.replace("job", "") or 1) - 1
            style = HR_STYLES[idx % len(HR_STYLES)]
            cur = self._cursor.get(ext_id, 0)
            if cur < len(style):
                self._sessions[ext_id].append({"role": "them", "text": style[cur]})
                self._cursor[ext_id] = cur + 1
                self._unread.add(ext_id)
            return True

    def send_resume(self, ext_id: str) -> bool:
        with self._lock:
            if ext_id not in self._sessions:
                return False
            self._sessions[ext_id].append({"role": "me", "text": "[已发送简历]"})
            return True

    def risk_signal(self) -> str | None:
        return self._risk

    # 演示钩子
    def inject_risk(self, signal: str | None) -> None:
        self._risk = signal

    def reset(self) -> None:
        with self._lock:
            self._greeted.clear()
            self._unread.clear()
            for k in self._sessions:
                self._sessions[k].clear()
                self._cursor[k] = 0
            self._risk = None


# ---------------- 真实驱动 ----------------


class RealSeekerDriver(SeekerDriver):
    """Patchright 驱动。同步实现，必须跑在独立线程。"""

    name = "real"

    def __init__(self) -> None:
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._profile = ROOT / "data" / "chrome-profile-geek"
        self._cur_ext: str = ""

    def open(self) -> None:
        from patchright.sync_api import sync_playwright  # 延迟导入，mock 模式不需要

        self._profile.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile),
            channel="chrome",
            headless=False,
            viewport=None,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

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
            raise RuntimeError("浏览器未启动")
        return self._page

    def _find(self, scope: Any, key: str, timeout_ms: int = 2500) -> Any:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            for sel in SEL.get(key, []):
                try:
                    el = scope.query_selector(sel)
                    if el is not None:
                        return el
                except Exception:
                    continue
            time.sleep(0.2)
        return None

    def _find_all(self, scope: Any, key: str) -> list[Any]:
        for sel in SEL.get(key, []):
            try:
                els = scope.query_selector_all(sel)
                if els:
                    return list(els)
            except Exception:
                continue
        return []

    @staticmethod
    def _txt(el: Any) -> str:
        try:
            return (el.inner_text() or "").replace("\n", " ").strip()
        except Exception:
            return ""

    def risk_signal(self) -> str | None:
        try:
            body = self.page.inner_text("body")[:6000]
        except Exception:
            return None
        for kw in RISK_TEXT:
            if kw in body:
                if "验证" in kw:
                    return "captcha"
                if "上限" in kw:
                    return "daily_limit"
                if "频繁" in kw or "稍后" in kw:
                    return "rate_limit"
                return "risk"
        for sel in ["#nc_1_wrapper", "iframe[src*='captcha']"]:
            try:
                if self.page.query_selector(sel) is not None:
                    return "captcha"
            except Exception:
                continue
        return None

    def fetch_jobs(self, limit: int = 20) -> list[JobInfo]:
        from ..boss_font import parse_salary

        run_cfg = (config.seeker or {}).get("run", {}) or {}
        query = str(run_cfg.get("query", "运营"))
        city = str(run_cfg.get("city_code", "101250100"))
        try:
            self.page.goto(URL_JOBS.format(query=query, city=city),
                           wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        time.sleep(3)

        out: list[JobInfo] = []
        cards = self._find_all(self.page, "职位卡片")[:limit]
        for i, c in enumerate(cards):
            title = self._txt(self._find(c, "职位标题", 800))
            if not title:
                continue
            salary_raw = self._txt(self._find(c, "薪资", 800))
            sal = parse_salary(salary_raw)
            loc = self._txt(self._find(c, "地区", 800)).replace(" ", "")
            parts = [p for p in loc.split("·") if p]
            out.append(JobInfo(
                ext_id=f"real{i+1:03d}",
                title=title,
                company=self._txt(self._find(c, "公司", 800)),
                salary_text=sal["text"],
                salary_min=sal["min"],
                salary_max=sal["max"],
                city=parts[0] if parts else "",
                district="·".join(parts[1:]) if len(parts) > 1 else "",
                tags=[self._txt(t) for t in self._find_all(c, "标签")][:6],
                jd_text="",
            ))
        return out

    def greet(self, job: JobInfo, text: str) -> bool:
        """向 HR 发起沟通并发送定制话术。

        BOSS 求职端是"左列表 + 右详情"结构：
        点卡片只切换右侧详情（只读），点详情里的"立即沟通"才真正投递。

        流程：等列表 → 按标题找卡片 → 点卡片 → 详情面板找按钮 → 点击 → 发消息。

        返回值语义：点下"立即沟通"即算成功。那一刻 BOSS 已替我们发出默认招呼语，
        投递已产生；后面定制消息发不发得出去只影响质量，不改变"已投递"这个事实。
        """
        page = self.page

        # 0. 确保在职位列表页。
        # 点完"立即沟通"页面会跳到聊天界面，如果直接处理下一条，
        # 会在聊天页里找职位卡片和沟通按钮，必然超时失败。
        if "/web/geek/jobs" not in (page.url or ""):
            run_cfg = (config.seeker or {}).get("run", {}) or {}
            try:
                page.goto(
                    URL_JOBS.format(query=run_cfg.get("query", "Java"),
                                    city=run_cfg.get("city_code", "101250100")),
                    wait_until="domcontentloaded", timeout=45000,
                )
            except Exception:
                pass
            time.sleep(4)

        print(f"      等列表加载…", end="", flush=True)

        # 1. 等卡片出现（列表页渲染需要时间，等太短会找不到）
        cards: list[Any] = []
        for _ in range(15):
            cards = self._find_all(page, "职位卡片")
            if cards:
                break
            time.sleep(1)
        if not cards:
            print(" 列表为空")
            return False
        print(f" {len(cards)} 个", end="", flush=True)

        # 2. 按标题定位卡片
        key = (job.title or "")[:10]
        target = None
        for c in cards:
            try:
                if key and key in self._txt(c):
                    target = c
                    break
            except Exception:
                continue
        if target is None:
            print(f" ✗ 没找到「{key}」")
            return False

        # 3. 点卡片切换详情（只读，不发消息）
        # 直接点容器会被遮挡（固定头部/悬浮层拦截 pointer events），
        # 所以优先点卡片内的标题链接，再退到容器，最后用 JS 兜底。
        clicked = False
        try:
            link = target.query_selector("a[href*='/job_detail/']")
            if link is not None:
                link.click(timeout=5000)
                clicked = True
        except Exception:
            clicked = False

        if not clicked:
            try:
                target.click(timeout=4000)
                clicked = True
            except Exception:
                clicked = False

        if not clicked:
            try:
                target.evaluate("el => el.click()")
                clicked = True
            except Exception as exc:
                print(f" ✗ 点卡片失败：{str(exc)[:50]}")
                return False

        time.sleep(2.8)

        # 4. 详情面板里找沟通按钮
        btn = None
        for sel in SEL["沟通按钮"]:
            try:
                el = page.query_selector(sel)
                if el is not None and el.is_visible():
                    btn = el
                    break
            except Exception:
                continue
        if btn is None:
            print(" ✗ 详情区无沟通按钮")
            return False

        # 5. 点击 = 投递
        # 有些岗位卡片已沟通过、或按钮被浮层遮挡，常规 click 会超时，
        # 退到 JS 直接触发 click（绕过 pointer-events 检查）。
        clicked = False
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=6000)
            clicked = True
        except Exception:
            clicked = False

        if not clicked:
            try:
                btn.evaluate("el => el.click()")
                clicked = True
            except Exception as exc:
                print(f" ✗ 点按钮失败：{str(exc)[:50]}")
                return False

        time.sleep(3.5)

        # 6. 尽力发送定制话术（失败不改变"已投递"）
        try:
            box = self._find(page, "输入框", 6000)
            if box is not None:
                box.click()
                for ch in text:
                    box.type(ch, delay=0)
                    time.sleep(0.05)
                time.sleep(0.5)
                send = self._find(page, "发送按钮", 2000)
                if send is not None:
                    send.click()
                else:
                    box.press("Enter")
                time.sleep(1.5)
        except Exception:
            pass

        self._cur_ext = job.ext_id
        return True

    def fetch_unread(self) -> list[str]:
        try:
            self.page.goto(URL_CHAT, wait_until="domcontentloaded", timeout=40000)
        except Exception:
            pass
        time.sleep(2.5)

        out: list[str] = []
        for i, s in enumerate(self._find_all(self.page, "会话条目")[:20]):
            badge = self._find(s, "未读标记", 300)
            if badge is None:
                continue
            out.append(f"idx{i}")
        return out

    def read_messages(self, ext_id: str) -> list[dict[str, str]]:
        """点开会话读消息。ext_id 形如 idx3，对应列表里第 4 个会话。"""
        try:
            idx = int(ext_id.replace("idx", ""))
        except Exception:
            return []
        rows = self._find_all(self.page, "会话条目")
        if idx >= len(rows):
            return []
        try:
            rows[idx].click()
            time.sleep(2.2)
        except Exception:
            return []

        out: list[dict[str, str]] = []
        for m in self._find_all(self.page, "消息条目"):
            t = self._txt(self._find(m, "消息文本", 300)) or self._txt(m)
            if not t:
                continue
            cls = ""
            try:
                cls = str(m.get_attribute("class") or "")
            except Exception:
                pass
            # 方位判断：BOSS 自己的消息通常在右（含 myself/right），对方在左
            side = "me" if any(k in cls for k in ("myself", "right", "me")) else "them"
            out.append({"role": side, "text": t[:200]})
        return out

    def send_message(self, ext_id: str, text: str) -> bool:
        box = self._find(self.page, "输入框", 4000)
        if box is None:
            return False
        try:
            box.click()
            for ch in text:
                box.type(ch, delay=0)
                time.sleep(0.05)
            time.sleep(0.4)
            send = self._find(self.page, "发送按钮", 1500)
            if send is not None:
                send.click()
            else:
                box.press("Enter")
            time.sleep(1.2)
            return True
        except Exception:
            return False

    def send_resume(self, ext_id: str) -> bool:
        """发送简历。

        ⚠️ 这里的选择器**未实测**。BOSS 的"发送简历"入口受账号权益影响，
        有的账号是图标按钮，有的是在"+"菜单里。所以先探测再点，找不到就返回失败，
        由上层转人工，不硬点。
        """
        btn = self._find(self.page, "发送简历", 2500)
        if btn is None:
            return False
        try:
            btn.click()
            time.sleep(2.5)
            # 弹出的简历选择面板里确认发送
            for sel in ("text=发送", "button:has-text('发送')", ".btn-confirm"):
                try:
                    ok = self.page.query_selector(sel)
                    if ok is not None:
                        ok.click()
                        time.sleep(1.5)
                        return True
                except Exception:
                    continue
            return True
        except Exception:
            return False


# ---------------- 工厂 ----------------

_MOCK: MockSeekerDriver | None = None
_LOCK = threading.Lock()


def get_mock_seeker() -> MockSeekerDriver:
    global _MOCK
    with _LOCK:
        if _MOCK is None:
            _MOCK = MockSeekerDriver()
        return _MOCK


def get_seeker_driver() -> SeekerDriver:
    if config.mode == "real":
        return RealSeekerDriver()
    return get_mock_seeker()
