"""猎聘简历采集 · 单份。

**功能**：把一个会话里的在线简历，导出成三种形态
1. `resume.html` —— 原始 HTML 快照（保真，便于事后追溯）
2. `resume.pdf`  —— PDF 文件（可直接发给他人）
3. `resume.json` —— 结构化字段（可入库、可检索）

**重要前提（2026-11-04 实测）**：
猎聘没有「下载简历」按钮。简历是 `im-ui-resume-container` 渲染的 HTML，
只能在线看。所以"下载"是我们自己把 HTML 抓下来再转成文件。

**风控纪律**：本脚本一次只处理一个人，全程走 RiskGuard 的随机节奏，
不做批量遍历（曾经 30 个会话连点触发验证码）。

用法：
    python -m scripts.collect_resume --name 蔡示例
    python -m scripts.collect_resume --name 蔡示例 --no-pdf
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.browser.liepin_guard import RiskGuard, click_element  # noqa: E402

CDP_PORT = 53471
URL_IM = "https://h.liepin.com/im/showmsgnewpage"
OUT_ROOT = ROOT / "data" / "resumes"

CONV_ITEM = ".im-ui-contact-info"
RESUME_PANEL = ".im-ui-resume-container, .im-ui-resume-wrap"


def banner(t: str) -> None:
    print(f"\n{'─' * 60}\n{t}\n{'─' * 60}")


def open_conversation(pg: Any, guard: RiskGuard, name: str) -> bool:
    """点开会话。

    必须用真实鼠标坐标：实测 `el.click()` 和 `el.evaluate(click)` 对
    猎聘的会话项都无效，只有鼠标事件能选中。
    """
    for attempt in range(8):
        items = pg.query_selector_all(CONV_ITEM)
        for it in items:
            try:
                if name not in (it.inner_text() or ""):
                    continue
                try:
                    it.scroll_into_view_if_needed(timeout=2500)
                except Exception:
                    pass
                time.sleep(0.8)
                box = it.bounding_box()
                if not box:
                    continue
                vh = (pg.viewport_size or {}).get("height", 900)
                if not (0 < box["y"] < vh - 80):
                    continue
                pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                guard.after_action()
                time.sleep(4)
                return True
            except Exception:
                continue
        pg.mouse.wheel(0, 400)
        time.sleep(1.5)
    return False


def grab_resume(pg: Any, guard: RiskGuard) -> dict[str, Any] | None:
    """抓取右侧简历面板的 HTML 与文本。"""
    panels = pg.query_selector_all(RESUME_PANEL)
    if not panels:
        return None
    # 取内容最长的那个（有预览卡 + 完整面板两个）
    best = None
    for p in panels:
        try:
            if not p.is_visible():
                continue
            txt = p.inner_text() or ""
            if best is None or len(txt) > len(best[1]):
                best = (p, txt)
        except Exception:
            continue
    if best is None:
        return None
    el, text = best

    html = ""
    try:
        html = el.inner_html()
    except Exception:
        pass

    return {"text": text, "html": html, "len": len(text)}


def parse_fields(text: str) -> dict[str, Any]:
    """从简历文本里抽结构化字段。

    猎聘简历的文本是三行一组：
        中国移动系统集成有限公司(中国移动雄安产业研究院)   ← 公司
        GIS/CIM研发负责人                                ← 职位
        2019.06-至今 (7年5个月)                          ← 时间

    坑：一开始只取了"时间行的上一行"当公司职位，导致公司名丢失、
    项目混进工作经历。正确做法是按大标题分段，再按"上两行 + 时间行"配对。
    """
    out: dict[str, Any] = {}
    lines = [x.strip() for x in (text or "").split("\n") if x.strip()]

    # ---- 头部 ----
    m = re.search(r"求职意向\s*\n?([^\n]+)", text)
    if m:
        out["期望职位"] = m.group(1).strip()
    m = re.search(r"·\s*([^\s·]+)\s*\n?\s*(\d+-\d+k)", text)
    if m:
        out["期望城市"] = m.group(1)
        out["期望薪资"] = m.group(2)
    m = re.search(r"(博士|硕士|本科|大专)", text)
    if m:
        out["学历"] = m.group(1)
    m = re.search(r"工作\s*(\d+)\s*年", text)
    if m:
        out["工作年限"] = m.group(1) + "年"

    # ---- 按大标题分段 ----
    TIME_RE = re.compile(r"^\d{4}\.\d{2}\s*-\s*(\d{4}\.\d{2}|至今)")

    def split_sections(ls: list[str]) -> dict[str, list[str]]:
        secs: dict[str, list[str]] = {}
        cur = "其他"
        for ln in ls:
            if ln in ("工作经历", "项目经历", "教育经历", "求职意向"):
                cur = ln
                secs.setdefault(cur, [])
                continue
            secs.setdefault(cur, []).append(ln)
        return secs

    secs = split_sections(lines)

    # 工作经历：公司 + 职位 + 时间
    def pairs(ls: list[str], name_key: str, role_key: str) -> list[dict[str, str]]:
        res = []
        for k, ln in enumerate(ls):
            if not TIME_RE.match(ln):
                continue
            role = ls[k - 1] if k >= 1 else ""
            name = ls[k - 2] if k >= 2 else ""
            if len(name) > 45 or TIME_RE.match(name):
                name = ""
            item = {"时间": ln}
            if name:
                item[name_key] = name
            if role:
                item[role_key] = role[:30]
            res.append(item)
        return res

    works = pairs(secs.get("工作经历", []), "公司", "职位")
    if works:
        out["工作经历"] = works[:6]

    projs = pairs(secs.get("项目经历", []), "项目", "角色")
    if projs:
        out["项目经历"] = projs[:10]

    # 教育：学校 + 下一行详情
    el = secs.get("教育经历", [])
    edus = []
    for k, ln in enumerate(el):
        if any(x in ln for x in ("大学", "学院", "学校")) and len(ln) < 45:
            nxt = el[k + 1] if k + 1 < len(el) else ""
            edus.append({"学校": ln, **({"详情": nxt[:40]} if nxt else {})})
    if edus:
        out["教育经历"] = edus[:3]

    # 技能：从全文抓，范围放宽
    skills = re.findall(
        r"\b(Java|Python|Go|C\+\+|Spring\s*Cloud|Spring|MySQL|Oracle|PostgreSQL|Redis|"
        r"Kafka|Hadoop|Spark|GIS|CIM|BIM|微服务|分布式|架构设计|团队管理|项目管理|"
        r"数字孪生|Nacos|Docker|Kubernetes)\b",
        text,
    )
    out["技能"] = list(dict.fromkeys(x.replace(" ", "") for x in skills))[:14]

    return out


def to_pdf(pg: Any, html: str, out_path: Path) -> bool:
    """把抓到的 HTML 渲染成 PDF。

    用新标签页 set_content 再 page.pdf()，比第三方库更保真
    （Chromium 自己的排版引擎）。
    """
    page2 = None
    try:
        page2 = pg.context.new_page()
        wrapped = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
         font-size: 12px; line-height: 1.7; color: #222; padding: 24px; }}
  * {{ box-sizing: border-box; }}
  img {{ max-width: 100%; }}
  .im-ui-resume-container, .im-ui-resume-wrap {{ max-width: 100% !important;
         height: auto !important; max-height: none !important; overflow: visible !important; }}
  h1,h2,h3 {{ font-size: 15px; margin: 12px 0 6px; }}
</style></head>
<body>{html}</body></html>"""
        page2.set_content(wrapped, wait_until="domcontentloaded", timeout=20000)
        time.sleep(1.5)
        page2.pdf(path=str(out_path), format="A4",
                  margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
                  print_background=True)
        return True
    except Exception as exc:
        print(f"  ⚠ PDF 生成失败：{str(exc)[:70]}")
        return False
    finally:
        try:
            if page2 is not None:
                page2.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="候选人姓名（会话列表里的显示名）")
    ap.add_argument("--no-pdf", action="store_true", help="跳过 PDF 生成")
    args = ap.parse_args()

    from patchright.sync_api import sync_playwright

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_ROOT / f"{args.name}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        ctx = b.contexts[0]
        pg = next((x for x in ctx.pages if "h.liepin.com" in (x.url or "")), None)
        if pg is None:
            print("✗ 找不到猎聘页面")
            return 1

        guard = RiskGuard(pg, max_actions=8)

        banner(f"采集：{args.name}")
        if guard.check():
            print(f"✗ 当前已在风控状态（{guard.tripped}），先人工验证")
            return 1

        print("→ 打开沟通页")
        pg.goto(URL_IM, wait_until="domcontentloaded", timeout=45000)
        guard.after_nav()

        print(f"→ 定位会话「{args.name}」")
        if not open_conversation(pg, guard, args.name):
            print("✗ 没能点开会话")
            return 1
        print("  ✓ 会话已打开")
        time.sleep(4)

        if guard.check():
            print(f"✗ 触发风控（{guard.tripped}）")
            return 1

        banner("抓取简历")
        data = grab_resume(pg, guard)
        if not data or data["len"] < 80:
            print("✗ 没抓到简历内容（可能会话未选中，或对方没发简历）")
            pg.screenshot(path=str(out_dir / "debug.png"))
            print(f"  调试截图：{out_dir / 'debug.png'}")
            return 1

        print(f"  ✓ 正文 {data['len']} 字，HTML {len(data['html'])} 字符")
        print(f"  预览：{data['text'][:120].replace(chr(10), ' / ')}")

        # 1) 原始 HTML 快照
        (out_dir / "resume.html").write_text(
            f"<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
            f"<body>{data['html']}</body></html>",
            encoding="utf-8",
        )
        print(f"  ✓ resume.html")

        # 2) 纯文本
        (out_dir / "resume.txt").write_text(data["text"], encoding="utf-8")
        print(f"  ✓ resume.txt")

        # 3) 结构化
        fields = parse_fields(data["text"])
        fields["_采集时间"] = stamp
        fields["_来源"] = "h.liepin.com/im"
        fields["_姓名"] = args.name
        (out_dir / "resume.json").write_text(
            json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  ✓ resume.json（{len([k for k in fields if not k.startswith('_')])} 个字段）")

        # 4) PDF
        if not args.no_pdf:
            pdf_path = out_dir / "resume.pdf"
            if to_pdf(pg, data["html"], pdf_path):
                size = pdf_path.stat().st_size
                print(f"  ✓ resume.pdf（{size / 1024:.0f} KB）")

        # 5) 整页截图存档
        try:
            pg.screenshot(path=str(out_dir / "screen.png"))
            print(f"  ✓ screen.png")
        except Exception:
            pass

        banner("结果")
        print(f"  目录：{out_dir}")
        print(f"  {guard.summary()}")
        print()
        print("  结构化字段：")
        for k, v in fields.items():
            if k.startswith("_"):
                continue
            s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            print(f"    {k}: {s[:70]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
