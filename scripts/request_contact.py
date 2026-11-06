"""批量「索要手机 / 索要微信」。

**为什么有这个脚本（客户描述的真实流程）**
正常招聘沟通是：选职位 → 开聊发话术 → **索要手机和微信** → 对方回复后导出简历。
其中「索要手机」「索要微信」是猎聘聊天窗里的现成按钮
（`im-ui-action-button`，会话打开后出现在消息区上方），
一个一个手点太累，这个脚本对指定的人批量点。

**注意**：索要动作对方是能看到的（一条系统消息），
对同一个人重复索要没有意义，所以默认跳过最近已经索要过的。

用法：
    python -m scripts.request_contact --names "王**|32岁,刘**|29岁"
    python -m scripts.request_contact --limit 5 --what phone,wechat
    python -m scripts.request_contact --limit 5 --what phone,wechat --dry
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.browser.liepin_guard import RiskGuard  # noqa: E402

CDP_PORT = 53471
URL_IM = "https://h.liepin.com/im/showmsgnewpage"
LOG_FILE = ROOT / "data" / "request_log.json"

CONV_ITEM = ".im-ui-contact-info"
# 消息区上方的动作按钮组
ACTION_BTN = "im-ui-action-button"
VALID_WHAT = ("phone", "wechat", "resume")


def banner(t: str) -> None:
    print(f"\n{'─' * 60}\n{t}\n{'─' * 60}")


def load_log() -> list[dict[str, Any]]:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_log(log: list[dict[str, Any]]) -> None:
    LOG_FILE.write_text(json.dumps(log[-500:], ensure_ascii=False, indent=2), encoding="utf-8")


def open_conv(pg: Any, guard: RiskGuard, name: str) -> bool:
    for _ in range(8):
        for it in pg.query_selector_all(CONV_ITEM):
            try:
                if name not in (it.inner_text() or ""):
                    continue
                it.scroll_into_view_if_needed(timeout=2500)
                time.sleep(0.6)
                box = it.bounding_box()
                if not box:
                    continue
                vh = (pg.viewport_size or {}).get("height", 900)
                if not (0 < box["y"] < vh - 80):
                    continue
                pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                time.sleep(3.5)
                return True
            except Exception:
                continue
        pg.mouse.wheel(0, 350)
        time.sleep(1.2)
    return False


def click_action(pg: Any, which: str) -> str:
    """点「索要手机/索要微信/索要简历」按钮。

    返回：'clicked' | 'notfound' | 'confirm:xxx'（弹了确认框）
    """
    label = {"phone": "索要手机", "wechat": "索要微信", "resume": "索要简历"}[which]

    els = pg.query_selector_all(f"[class*={ACTION_BTN}]")
    for el in els:
        try:
            if not el.is_visible():
                continue
            txt = el.inner_text() or ""
            if label not in txt:
                continue
            el.scroll_into_view_if_needed(timeout=2500)
            time.sleep(0.5)
            box = el.bounding_box()
            if not box:
                continue
            pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            time.sleep(2.5)

            # 确认框是猎聘自己的 ant-im-* 变体，不是标准 antd
            for sel in (".ant-im-modal-confirm-btns button.ant-im-btn-primary",
                        ".ant-modal-confirm-btns button.ant-btn-primary"):
                confirm = pg.query_selector(sel)
                try:
                    if confirm and confirm.is_visible():
                        cb = confirm.bounding_box()
                        pg.mouse.click(cb["x"] + cb["width"] / 2, cb["y"] + cb["height"] / 2)
                        time.sleep(1.5)
                        return "clicked+confirm"
                except Exception:
                    continue
            return "clicked"
        except Exception:
            continue
    return "notfound"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default="", help="指定人（姓名|年龄 唯一键，逗号分隔）；空 = 按会话顺序")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--what", default="phone,wechat", help="phone / wechat / resume，逗号分隔")
    ap.add_argument("--dry", action="store_true", help="只检查按钮在不在，不真点")
    args = ap.parse_args()

    whats = [x.strip() for x in args.what.split(",") if x.strip() in VALID_WHAT]
    if not whats:
        print("✗ --what 里没有合法项")
        return 1

    from patchright.sync_api import sync_playwright

    log = load_log()
    # 最近 7 天已真实索要过的人跳过（dry 演练不算）
    recently = {x["name"] for x in log if x.get("ok") and not x.get("dry") and
                datetime.now().timestamp() - x.get("ts", 0) < 7 * 86400}

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        ctx = b.contexts[0]
        pg = next((x for x in ctx.pages if "h.liepin.com" in (x.url or "")), None)
        if pg is None:
            print("✗ 找不到猎聘页面")
            return 1

        guard = RiskGuard(pg, max_actions=30)
        if guard.check():
            print(f"✗ 风控状态（{guard.tripped}）")
            return 1

        pg.goto(URL_IM, wait_until="domcontentloaded", timeout=45000)
        guard.after_nav()

        want = [x.strip() for x in args.names.split(",") if x.strip()]
        names: list[str] = []
        if want:
            # 唯一键 姓名|年龄 → 只取姓名用于会话定位
            names = [x.split("|")[0] for x in want]
        else:
            for it in pg.query_selector_all(CONV_ITEM):
                n = (it.inner_text() or "").split("\n")[0].strip()
                if n and not n.isdigit():
                    names.append(n)
            names = names[: args.limit]

        # 去掉最近索要过的
        todo = [n for n in names if n not in recently]
        skipped = [n for n in names if n in recently]
        if skipped:
            print(f"跳过最近已索要：{', '.join(skipped)}")

        banner(f"待处理 {len(todo)} 人 × {len(whats)} 项")
        if args.dry:
            print("(dry 模式：只检查按钮，不真点)")

        results = []
        for i, n in enumerate(todo, 1):
            if guard.blocked:
                print(f"\n⚠ 停止：{guard.summary()}")
                break
            if not guard.before_action(f"{i}/{len(todo)}"):
                break

            print(f"\n  [{i}/{len(todo)}] {n}")
            if not open_conv(pg, guard, n):
                print("    点不开会话")
                continue
            guard.after_action()

            for w in whats:
                if args.dry:
                    label = {"phone": "索要手机", "wechat": "索要微信", "resume": "索要简历"}[w]
                    found = any(label in (el.inner_text() or "")
                                for el in pg.query_selector_all(f"[class*={ACTION_BTN}]")
                                if el.is_visible())
                    print(f"    {label}: {'按钮在' if found else '✗ 没找到'}")
                    results.append({"name": n, "what": w, "ok": found, "dry": True, "ts": time.time()})
                    continue

                r = click_action(pg, w)
                ok = r.startswith("clicked")
                print(f"    {'索要' + {'phone': '手机', 'wechat': '微信', 'resume': '简历'}[w]}: {r}")
                results.append({"name": n, "what": w, "ok": ok, "result": r, "ts": time.time()})
                time.sleep(random.uniform(1.5, 3))

            log.extend([x for x in results if x["name"] == n])
            save_log(log)
            time.sleep(random.uniform(3, 6))

    okn = sum(1 for x in results if x.get("ok"))
    banner(f"完成：{okn}/{len(results)} 项成功")
    print(f"  日志：{LOG_FILE}")
    print(f"  {guard.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
