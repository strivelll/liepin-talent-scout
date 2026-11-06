"""写操作探查：验证"打招呼"和"读回复"这两个真实动作。

**风险说明（先看这段）**

求职端的"打招呼"不是模拟——它会真的给真实 HR 发一条消息。所以本脚本按三段式设计：

1. `explore`：纯只读。只找按钮和输入框在哪，**不点任何东西**。
2. `dry`：预演。点开聊天框、把话术填进输入框，但**不按发送**，然后清空。
3. `send`：实际发送。必须显式加 `--confirm` 才执行，且默认只发 1 条。

每段都记录风控信号，发送后还会等一段时间再复查一次页面，
用来判断"消息发出去了没有"和"账号有没有被限制"。

**为什么拆三段**

招聘端最终也要执行发送动作，如果求职端连"输入框在哪"都找不到，
招聘端那套选择器必然是错的。但发送是不可撤销的，所以先把不确定性
消化在只读阶段，是唯一稳的顺序。

用法：
    python -m scripts.probe_write explore
    python -m scripts.probe_write dry   --text "你好，看到贵司在招运营，方便聊两句吗？"
    python -m scripts.probe_write send  --text "..." --confirm
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROFILE_DIR = ROOT / "data" / "chrome-profile-geek"
OUT_DIR = ROOT / "data" / "probe-write"

# 求职端"打招呼"链路的选择器（多候选，按顺序试）
SEL: dict[str, list[str]] = {
    # 职位列表/详情页上的沟通入口
    "沟通按钮": [
        ".btn-startchat",
        ".op-btn-chat",
        "a[class*='startchat']",
        "button[class*='startchat']",
        "[class*='btn-chat']",
        "[class*='start-chat']",
        ".job-detail-op .btn",
    ],
    # 聊天页
    "会话列表": [
        ".chat-user-list",
        ".user-list",
        "[class*='chat-user']",
        "[class*='friend-list']",
        "[class*='session-list']",
    ],
    "会话条目": [
        ".chat-user-list li",
        ".user-list li",
        "[class*='user-item']",
        "[class*='session-item']",
    ],
    "未读标记": [
        ".badge-count",
        "[class*='unread']",
        "[class*='badge']",
    ],
    "会话名称": [
        ".name-text",
        ".user-name",
        "[class*='name']",
    ],
    "消息列表": [
        ".chat-message-list",
        ".message-list",
        "[class*='message-list']",
        "[class*='msg-list']",
    ],
    "消息条目": [
        ".chat-message-list li",
        ".message-list li",
        "[class*='message-item']",
        "[class*='msg-item']",
    ],
    "消息文本": [
        ".text",
        ".message-text",
        "[class*='text']",
        "[class*='content']",
    ],
    "输入框": [
        ".chat-input",
        "[contenteditable='true']",
        "textarea[class*='input']",
        ".input-area textarea",
        ".chat-editor",
    ],
    "发送按钮": [
        ".send-message",
        ".btn-send",
        "button[class*='send']",
        "[class*='send-btn']",
    ],
}

RISK_TEXT = ["验证码", "安全验证", "操作过于频繁", "请稍后再试", "账号异常", "访问受限", "今日沟通已达上限"]

URL_JOB = "https://www.zhipin.com/web/geek/job-recommend"
URL_CHAT = "https://www.zhipin.com/web/geek/chat"


def _console() -> None:
    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            try:
                if s.isatty():
                    s.reconfigure(encoding="gbk", errors="replace")
            except Exception:
                pass


def banner(t: str) -> None:
    print(f"\n{'─' * 66}\n{t}\n{'─' * 66}")


def find(page: Any, name: str, timeout_ms: int = 2500) -> tuple[Any, str]:
    """返回 (元素, 命中的选择器)。找不到返回 (None, "")。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in SEL.get(name, []):
            try:
                el = page.query_selector(sel)
                if el is not None:
                    return el, sel
            except Exception:
                continue
        time.sleep(0.2)
    return None, ""


def find_all(page: Any, name: str) -> tuple[list[Any], str]:
    for sel in SEL.get(name, []):
        try:
            els = page.query_selector_all(sel)
            if els:
                return list(els), sel
        except Exception:
            continue
    return [], ""


def text_of(el: Any) -> str:
    try:
        return (el.inner_text() or "").replace("\n", " ").strip()
    except Exception:
        return ""


def risk(page: Any) -> list[str]:
    hits: list[str] = []
    try:
        body = page.inner_text("body")[:6000]
    except Exception:
        return hits
    for kw in RISK_TEXT:
        if kw in body:
            hits.append(kw)
    for sel in ["#nc_1_wrapper", "iframe[src*='captcha']", "[class*='captcha']", "[class*='verify']"]:
        try:
            if page.query_selector(sel) is not None:
                hits.append(f"控件:{sel}")
        except Exception:
            continue
    return hits


def ensure_login(page: Any, timeout_sec: int = 180) -> bool:
    """确认已登录。未登录时原地等扫码，不自动重试。"""
    def logged() -> bool | None:
        try:
            url = page.url.lower()
        except Exception:
            return None
        if "/web/user/" in url or "login" in url:
            return False
        try:
            body = page.inner_text("body")[:3000]
        except Exception:
            return None
        for kw in ("扫码登录", "手机号登录", "密码登录", "验证码登录"):
            if kw in body:
                return False
        return True

    state = logged()
    if state is True:
        print("✓ 已登录")
        return True

    banner("需要登录")
    print("请在浏览器窗口里扫码登录，登录后脚本自动继续。")
    deadline = time.time() + timeout_sec
    tick = 0
    while time.time() < deadline:
        if logged():
            print("\n✓ 检测到已登录")
            time.sleep(2)
            return True
        if tick % 5 == 0:
            remain = int(deadline - time.time())
            print(f"  等待登录中… 剩余 {remain}s")
        tick += 1
        time.sleep(3)
    print("✗ 登录超时")
    return False


def survey(page: Any, label: str) -> dict[str, Any]:
    """探查一个页面上所有目标元素的命中情况。"""
    banner(f"探查：{label}")
    result: dict[str, Any] = {"label": label, "url": "", "items": []}
    try:
        result["url"] = page.url
        print(f"  地址：{page.url}")
    except Exception:
        pass

    for name in SEL:
        if name in ("会话名称", "消息文本"):
            continue  # 这两个依附于容器，单独看命中数没意义
        els, sel = find_all(page, name)
        n = len(els)
        mark = "✓" if n else "✗"
        sample = text_of(els[0])[:34] if els else ""
        print(f"  {mark} {name:<8} {n:>3}  {sel:<28} {sample}")
        result["items"].append({"name": name, "hits": n, "selector": sel, "sample": sample})

    r = risk(page)
    if r:
        print(f"  ⚠ 风控信号：{'、'.join(r)}")
    result["risk"] = r
    return result


def explore(page: Any) -> dict[str, Any]:
    """只读探查：职位页找沟通按钮，消息页找会话与输入框。不点任何东西。"""
    data: dict[str, Any] = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pages": []}

    for url, label in ((URL_JOB, "职位推荐页"), (URL_CHAT, "消息页")):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
        except Exception as exc:
            print(f"  打开 {url} 异常（继续）：{exc}")
        time.sleep(3)
        data["pages"].append(survey(page, label))

    # 如果消息页有会话，点开第一个看消息结构（点会话是只读行为，不发送）
    sess, sel = find_all(page, "会话条目")
    if sess:
        banner("点开第一个会话，查看消息结构（只读）")
        try:
            sess[0].click()
            time.sleep(2.5)
            for name in ("消息列表", "消息条目", "消息文本"):
                els, s = find_all(page, name)
                print(f"  {'✓' if els else '✗'} {name:<8} {len(els):>3}  {s}")
                data.setdefault("chat_detail", []).append(
                    {"name": name, "hits": len(els), "selector": s}
                )
            box, bs = find(page, "输入框", 1500)
            btn, bts = find(page, "发送按钮", 1500)
            print(f"  {'✓' if box else '✗'} 输入框     {bs or '未找到'}")
            print(f"  {'✓' if btn else '✗'} 发送按钮   {bts or '未找到'}")
            data["chat_detail"].append({"name": "输入框", "hits": 1 if box else 0, "selector": bs})
            data["chat_detail"].append({"name": "发送按钮", "hits": 1 if btn else 0, "selector": bts})
        except Exception as exc:
            print(f"  点开会话失败：{exc}")
    else:
        print("\n  消息页没有会话（还没和人沟通过），聊天结构暂时测不到。")

    return data


def read_conversations(page: Any, detail: int = 3) -> list[dict[str, Any]]:
    """读取会话列表与最近消息。纯只读。

    这是招聘端"读候选人回复"的同一套逻辑，先在求职端验证通道。
    """
    try:
        page.goto(URL_CHAT, wait_until="domcontentloaded", timeout=40000)
    except Exception as exc:
        print(f"  打开消息页失败：{exc}")
        return []
    time.sleep(3)

    rows, sel = find_all(page, "会话条目")
    if not rows:
        print("  没有会话")
        return []

    banner(f"会话列表（{len(rows)} 个，来自 {sel}）")
    out: list[dict[str, Any]] = []
    for el in rows[:12]:
        raw = text_of(el)
        unread_el, _ = (None, "")
        try:
            for s2 in SEL["未读标记"]:
                u = el.query_selector(s2)
                if u is not None:
                    unread_el = u
                    break
        except Exception:
            pass
        name = ""
        try:
            for s2 in SEL["会话名称"]:
                n = el.query_selector(s2)
                if n is not None:
                    name = text_of(n)
                    break
        except Exception:
            pass
        out.append({
            "name": name or raw[:16],
            "raw": raw[:120],
            "unread": unread_el is not None,
            "unread_text": text_of(unread_el) if unread_el is not None else "",
        })
        flag = f"[未读 {out[-1]['unread_text']}]" if out[-1]["unread"] else "      "
        print(f"  {flag} {out[-1]['name'][:14]:<16} {raw[:52]}")

    # 点开前几个会话，读具体消息（只读）
    banner(f"打开前 {min(detail, len(rows))} 个会话读消息")
    for i, el in enumerate(rows[:detail]):
        try:
            el.click()
            time.sleep(2.2)
            msgs, msel = find_all(page, "消息条目")
            texts: list[dict[str, str]] = []
            for m in msgs:
                t = ""
                try:
                    for s2 in SEL["消息文本"]:
                        n = m.query_selector(s2)
                        if n is not None:
                            t = text_of(n)
                            break
                except Exception:
                    pass
                if not t:
                    t = text_of(m)[:120]
                if not t:
                    continue
                # 通过 class 里含的方位词粗判方向
                cls = ""
                try:
                    cls = str(m.get_attribute("class") or "")
                except Exception:
                    pass
                side = "them" if any(k in cls for k in ("friend", "left", "receive", "other")) else "me"
                texts.append({"side": side, "text": t[:160]})
            print(f"\n  会话 {i + 1}：{out[i]['name'][:20]}　消息 {len(texts)} 条（{msel}）")
            for t in texts[-4:]:
                who = "对方" if t["side"] == "them" else "我  "
                print(f"    [{who}] {t['text'][:70]}")
            out[i]["messages"] = texts
        except Exception as exc:
            print(f"  会话 {i + 1} 读取失败：{exc}")
    return out


def open_existing_chat(page: Any) -> tuple[bool, str]:
    """在消息页打开第一个已有会话。

    比点"立即沟通"安全得多：不会产生新的触达记录，也不会触发打招呼限额。
    用来验证"输入→发送→回显"这条通道本身是否被风控。
    """
    try:
        page.goto(URL_CHAT, wait_until="domcontentloaded", timeout=40000)
    except Exception as exc:
        return False, f"打开消息页失败：{exc}"
    time.sleep(3)

    sess, sel = find_all(page, "会话条目")
    if not sess:
        return False, "消息页没有会话（还没和任何人沟通过），无法用已有会话测试"

    try:
        sess[0].click()
        time.sleep(2.5)
        name = text_of(sess[0])[:30]
        return True, f"已打开会话：{name}（{sel}）"
    except Exception as exc:
        return False, f"点开会话失败：{exc}"


def open_first_chat(page: Any) -> tuple[bool, str]:
    """在职位页点第一个"沟通"按钮，进入聊天。返回是否成功与说明。

    注意：点击"立即沟通"通常会**同时发送一条默认招呼语**（BOSS 的产品行为）。
    所以这一步已经算写操作，只在 dry/send 模式里调用。
    """
    try:
        page.goto(URL_JOB, wait_until="domcontentloaded", timeout=40000)
    except Exception as exc:
        return False, f"打开职位页失败：{exc}"
    time.sleep(3)

    btn, sel = find(page, "沟通按钮", 5000)
    if btn is None:
        return False, "没找到沟通按钮，需要先校准 SEL['沟通按钮']"

    try:
        btn.click()
        time.sleep(3)
        return True, sel
    except Exception as exc:
        return False, f"点击失败：{exc}"


def send_text(page: Any, text: str) -> tuple[bool, str]:
    """在当前聊天页把文本填进输入框并发送。"""
    box, bsel = find(page, "输入框", 5000)
    if box is None:
        return False, f"没找到输入框（已试 {len(SEL['输入框'])} 个候选）"

    try:
        box.click()
        time.sleep(0.4)
        for ch in text:
            box.type(ch, delay=0)
            time.sleep(0.05)
    except Exception as exc:
        return False, f"输入失败：{exc}"

    time.sleep(0.6)
    btn, btsel = find(page, "发送按钮", 2500)
    try:
        if btn is not None:
            btn.click()
            return True, f"已点击发送按钮（{btsel}）"
        box.press("Enter")
        return True, "输入框内回车发送（未找到发送按钮）"
    except Exception as exc:
        return False, f"发送失败：{exc}"


def verify_sent(page: Any, text: str) -> dict[str, Any]:
    """发送后复查：消息是否出现在会话里、有没有风控。"""
    time.sleep(8)
    out: dict[str, Any] = {"risk": risk(page), "found_in_page": False}
    try:
        body = page.inner_text("body")
        # 只比对前 20 字，避免长文本被换行截断
        out["found_in_page"] = text[:20] in body if text else False
    except Exception:
        pass
    return out


def main() -> int:
    _console()
    ap = argparse.ArgumentParser(description="求职端写操作验证")
    ap.add_argument("mode", choices=["explore", "read", "dry", "send"],
                    help="explore=结构探查 / read=读取回复 / dry=预演不发送 / send=实际发送")
    ap.add_argument("--text", default="你好，看到贵司在招人，方便聊两句吗？", help="要发送的内容")
    ap.add_argument("--confirm", action="store_true", help="send 模式必须加此参数")
    ap.add_argument("--profile", default="", help="登录态目录")
    ap.add_argument("--detail", type=int, default=3, help="read 模式打开前几个会话读消息")
    ap.add_argument("--new-job", action="store_true",
                    help="在职位页点'立即沟通'（会产生新触达，风险更高）；默认在已有会话里操作")
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()

    global PROFILE_DIR
    if args.profile:
        PROFILE_DIR = Path(args.profile).expanduser()

    if args.mode == "send" and not args.confirm:
        print("✗ send 模式会给真实 HR 发消息，必须加 --confirm 才执行。")
        print("  建议顺序：explore → dry → send --confirm")
        return 1

    banner("求职端写操作验证")
    print(f"模式：{args.mode}")
    print("风险提示：'立即沟通'在 BOSS 上通常会同时发出一条默认招呼语，这已经是真实动作。")
    if args.mode != "explore":
        print("本脚本会记录每次动作前后的风控信号，用于判断是否被限制。\n")

    try:
        from patchright.sync_api import sync_playwright
    except Exception:
        print("✗ 未安装 patchright。请先运行 setup.bat，或用 .venv\\Scripts\\python.exe 执行。")
        return 1

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_DIR / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    pw = sync_playwright().start()
    ctx = None
    code = 0
    report: dict[str, Any] = {"mode": args.mode, "at": stamp, "text": args.text}
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            viewport=None,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.zhipin.com/web/geek/job-recommend",
                      wait_until="domcontentloaded", timeout=40000)
        except Exception:
            pass
        time.sleep(2)
        if not ensure_login(page):
            print("\n未登录，退出。登录后重跑即可（登录态会保存）。")
            return 2

        if args.mode == "read":
            report["conversations"] = read_conversations(page, detail=args.detail)
            try:
                page.screenshot(path=str(out_dir / "read.png"), full_page=False)
            except Exception:
                pass

        elif args.mode == "explore":
            report["explore"] = explore(page)
            try:
                page.screenshot(path=str(out_dir / "explore.png"), full_page=False)
            except Exception:
                pass

        elif args.mode == "dry":
            # 默认用已有会话预演：零新触达，只验证输入通道
            if args.new_job:
                ok, info = open_first_chat(page)
            else:
                ok, info = open_existing_chat(page)
            report["open_chat"] = {"ok": ok, "info": info, "via": "new_job" if args.new_job else "existing"}
            if not ok:
                print(f"✗ {info}")
                code = 2
            else:
                banner("已进入聊天页，开始预演（不会发送）")
                box, bsel = find(page, "输入框", 5000)
                if box is None:
                    print("✗ 没找到输入框")
                    code = 2
                else:
                    print(f"  ✓ 输入框：{bsel}")
                    btn, btsel = find(page, "发送按钮", 2000)
                    print(f"  {'✓' if btn else '✗'} 发送按钮：{btsel or '未找到（将用回车发送）'}")
                    try:
                        box.click()
                        for ch in args.text:
                            box.type(ch, delay=0)
                            time.sleep(0.05)
                        print(f"  已填入 {len(args.text)} 字，但**没有按发送**")
                        # 清空，避免残留
                        box.press("Control+A")
                        box.press("Delete")
                        print("  已清空输入框")
                    except Exception as exc:
                        print(f"  填入失败：{exc}")
                        code = 2
                    r = risk(page)
                    print(f"  风控信号：{'、'.join(r) if r else '未检测到'}")
                    report["dry"] = {"input": bsel, "send_btn": btsel, "risk": r}

        else:  # send
            if args.new_job:
                ok, info = open_first_chat(page)
            else:
                ok, info = open_existing_chat(page)
            report["open_chat"] = {"ok": ok, "info": info, "via": "new_job" if args.new_job else "existing"}
            if not ok:
                print(f"✗ {info}")
                code = 2
            else:
                before = risk(page)
                print(f"  发送前风控：{'、'.join(before) if before else '正常'}")
                sent, sinfo = send_text(page, args.text)
                print(f"  {'✓' if sent else '✗'} {sinfo}")
                chk = verify_sent(page, args.text)
                print(f"  消息出现在页面上：{'是' if chk['found_in_page'] else '否（可能异步渲染，需人工确认）'}")
                print(f"  发送后风控：{'、'.join(chk['risk']) if chk['risk'] else '未检测到'}")
                report["send"] = {"sent": sent, "info": sinfo, "before_risk": before, **chk}
                try:
                    page.screenshot(path=str(out_dir / "after_send.png"), full_page=False)
                except Exception:
                    pass

        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        banner("结果已保存")
        print(f"  {out_dir / 'report.json'}")

        if args.keep_open:
            banner("浏览器保持打开，按 Ctrl+C 结束")
            while True:
                time.sleep(5)

    except KeyboardInterrupt:
        print("\n已中断")
        code = 130
    except Exception as exc:
        print(f"\n✗ 执行异常：{exc}")
        import traceback

        traceback.print_exc()
        code = 1
    finally:
        try:
            if ctx is not None and not args.keep_open:
                ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
