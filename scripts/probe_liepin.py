"""猎聘猎头端（h.liepin.com）页面结构探针。

**定位**：这是开发前的校准工具，不是业务代码。

**安全边界**：
- 只读页面结构，不点「下载简历」「沟通」「获取联系方式」等消耗额度的按钮
- 搜索是可以点的（免费）
- 所有输出落盘，便于事后比对改版

**用法**：
    # 1. 只看找人页有哪些输入框和按钮
    python -m scripts.probe_liepin --inspect

    # 2. 执行一次搜索，输出结果卡片结构
    python -m scripts.probe_liepin --search "Java"

    # 3. 连自己的 profile（正式开发用），而不是连 liepin-cli 的浏览器
    python -m scripts.probe_liepin --own-profile --search "Java"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "liepin-probe"
# liepin-cli 的常驻浏览器调试端口；我们自己的 profile 用 0（直接启动）
CLI_CDP_PORT = int(os.environ.get("LIEPIN_CDP_PORT", "53471"))
OWN_PROFILE = ROOT / "data" / "chrome-profile-liepin"

URL_HOME = "https://h.liepin.com/"
URL_SEARCH = "https://h.liepin.com/search/getConditionItem"


def _console() -> None:
    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            try:
                if s.isatty():
                    s.reconfigure(encoding="gbk", errors="replace")
            except Exception:
                pass


def banner(t: str) -> None:
    print(f"\n{'─' * 64}\n{t}\n{'─' * 64}")


def connect(pw: Any, own: bool, keep_open: bool) -> tuple[Any, Any, bool]:
    """返回 (browser, page, 是否由我们启动)。"""
    if not own:
        try:
            b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CLI_CDP_PORT}")
            ctx = b.contexts[0]
            pg = next((x for x in ctx.pages if "liepin" in (x.url or "")), None)
            if pg is None:
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            print(f"✓ 已连接常驻浏览器（端口 {CLI_CDP_PORT}）")
            return b, pg, False
        except Exception as exc:
            print(f"✗ 连接常驻浏览器失败：{str(exc)[:80]}")
            print("  改用独立 profile 启动…")

    OWN_PROFILE.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(OWN_PROFILE),
        channel="chrome",
        headless=False,
        viewport=None,
        args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
    )
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    print(f"✓ 已用独立 profile 启动：{OWN_PROFILE}")
    return ctx, pg, True


def inspect(pg: Any) -> dict[str, Any]:
    """读出页面上的输入框和按钮。用最朴素的写法，避免复杂表达式在页面上报错。"""
    js_inputs = """
    () => {
      var out = [];
      var list = document.querySelectorAll('input, textarea');
      for (var i = 0; i < list.length; i++) {
        var e = list[i];
        if (!e.offsetParent) continue;
        out.push([e.tagName, e.type || '', String(e.className || '').substring(0, 60), String(e.placeholder || '')]);
      }
      return out.slice(0, 20);
    }"""
    js_buttons = """
    () => {
      var out = [];
      var list = document.querySelectorAll('button, a, span');
      for (var i = 0; i < list.length; i++) {
        var e = list[i];
        if (!e.offsetParent) continue;
        var t = String(e.innerText || '').trim();
        if (!t || t.length > 16) continue;
        var c = String(e.className || '');
        if (c.indexOf('btn') < 0 && c.indexOf('search') < 0 && e.tagName !== 'BUTTON') continue;
        out.push([e.tagName, c.substring(0, 50), t]);
      }
      return out.slice(0, 25);
    }"""
    out: dict[str, Any] = {"inputs": [], "buttons": []}
    try:
        for row in pg.evaluate(js_inputs) or []:
            out["inputs"].append({"tag": row[0], "type": row[1], "cls": row[2], "ph": row[3]})
    except Exception as exc:
        out["inputs_err"] = str(exc)[:120]
    try:
        for row in pg.evaluate(js_buttons) or []:
            out["buttons"].append({"tag": row[0], "cls": row[1], "text": row[2]})
    except Exception as exc:
        out["buttons_err"] = str(exc)[:120]
    return out


def find_container(pg: Any) -> list[dict[str, Any]]:
    """结构嗅探：找出子元素结构一致的容器，可能是结果列表。

    不依赖具体 class 名，靠"列表里每个元素长得一样"这个结构特征。
    """
    js = """
    () => {
      var out = [];
      var all = document.querySelectorAll('div, ul, section');
      for (var i = 0; i < all.length; i++) {
        var el = all[i];
        var kids = el.children;
        if (!kids || kids.length < 3 || kids.length > 80) continue;
        var keyOf = function(e) {
          var c = e.className;
          if (typeof c !== 'string') return '';
          return (c.trim().split(/\\s+/)[0]) || '';
        };
        var first = keyOf(kids[0]);
        if (!first) continue;
        var same = 0;
        for (var k = 0; k < kids.length; k++) if (keyOf(kids[k]) === first) same++;
        if (same < kids.length * 0.7) continue;
        out.push([
          String(el.className || '').substring(0, 60),
          first.substring(0, 50),
          kids.length,
          String(kids[0].innerText || '').replace(/\\s+/g, ' ').trim().substring(0, 90)
        ]);
      }
      out.sort(function(a, b) { return b[2] - a[2]; });
      return out.slice(0, 15);
    }"""
    try:
        rows = pg.evaluate(js) or []
    except Exception as exc:
        print(f"  嗅探失败：{str(exc)[:100]}")
        return []
    return [{"parentCls": r[0], "childCls": r[1], "count": r[2], "sample": r[3]} for r in rows]


def close_modal(pg: Any) -> None:
    """关掉"请输入搜索条件"之类的提示弹窗，否则会挡住后续操作。"""
    try:
        for sel in ("button:has-text('知道了')", ".ant-modal-close", "button:has-text('确定')"):
            el = pg.query_selector(sel)
            if el is not None and el.is_visible():
                el.click()
                print("✓ 已关闭提示弹窗")
                time.sleep(0.6)
                return
    except Exception:
        pass


def do_search(pg: Any, query: str) -> bool:
    """执行搜索。

    **关键**：关键词框是 Ant Design 的 Select（tags 模式），
    外层是 div.ant-select，真正的输入是内部 input.ant-select-selection-search-input。
    直接对 placeholder 调 click() 不会把焦点送进去，必须 JS 定位后 focus。

    已踩过的坑：点了 placeholder、type 了文字、按了回车，结果框里还是空的，
    前端校验弹"请输入搜索条件后再搜索"。
    """
    close_modal(pg)

    # 1. JS 定位关键词 select，并聚焦内部 input
    js_focus = """() => {
      var phs = document.querySelectorAll('.ant-select-selection-placeholder');
      for (var i = 0; i < phs.length; i++) {
        var t = String(phs[i].innerText || '');
        if (t.indexOf('搜职位') < 0) continue;
        var box = phs[i].closest('.ant-select');
        if (!box) continue;
        var inp = box.querySelector('input.ant-select-selection-search-input');
        if (inp) { inp.focus(); return 'focused:' + (inp.id || ''); }
        var sel = box.querySelector('.ant-select-selector');
        if (sel) { sel.click(); return 'clicked'; }
      }
      return 'not-found';
    }"""
    res = pg.evaluate(js_focus)
    print(f"  定位关键词框：{res}")
    if res == "not-found":
        print("✗ 没找到关键词选择器")
        return False
    time.sleep(0.8)

    # 2. 键盘输入
    pg.keyboard.type(query, delay=80)
    time.sleep(1.0)

    # 3. 回车成 tag
    pg.keyboard.press("Enter")
    time.sleep(1.0)

    # 4. 校验：看标签 / 看输入框的值
    state = pg.evaluate(
        """() => {
          var tags = [], inputs = [];
          document.querySelectorAll('.ant-select-selection-item').forEach(function(e){
            var t = String(e.innerText || '').trim(); if (t) tags.push(t);
          });
          document.querySelectorAll('input.ant-select-selection-search-input').forEach(function(e){
            if (e.value) inputs.push(e.value);
          });
          return {tags: tags, inputs: inputs};
        }"""
    )
    print(f"  标签：{state.get('tags')}　残留输入：{state.get('inputs')}")

    ok = any(query in str(x) for x in (state.get("tags") or [])) or \
         any(query in str(x) for x in (state.get("inputs") or []))
    if not ok:
        print("  ⚠ 关键词似乎没进去，中止（避免弹窗白白出现）")
        return False

    # 5. 点搜索
    btn = pg.query_selector("button.search-btn")
    if btn is None:
        print("✗ 没找到搜索按钮")
        return False
    try:
        btn.click(timeout=5000)
        print("✓ 已点「搜 索」")
    except Exception as exc:
        print(f"✗ 点搜索失败：{str(exc)[:70]}")
        return False
    time.sleep(10)
    close_modal(pg)

    # 结果可能在首屏下方，滚一遍再判断
    try:
        for _ in range(3):
            pg.mouse.wheel(0, 1200)
            time.sleep(0.8)
        pg.mouse.wheel(0, -4000)
        time.sleep(1.0)
    except Exception:
        pass
    print(f"  搜索后 URL：{pg.url[:90]}")
    return True


# ===== 卡片字段选择器 =====
#
# 猎聘的 class 分两类：
#   1. 语义化的（new-resume-personal-name / personal-detail-*）—— 稳定，直接用
#   2. CSS Module 的 hash（_40231kdlK 这种）—— 会随构建变化，**不能依赖**
#
# 所以策略是：稳定的用选择器，其余靠"整卡片文本 + 规则解析"。
# 这比精确选择器更抗改版——多加一个字段也不会崩，只是解析不到而已。

CARD_SEL = {
    # 只取外层卡片。曾经两个选择器都写，同一张卡被匹配两次，30 条变 60 条
    "容器": ".tlog-common-resume-card",
    "姓名": ".new-resume-personal-name",
    "年龄": ".personal-detail-age",
    "工作年限": ".personal-detail-workyears",
    "学历": ".personal-detail-edulevel",
    "地区": ".personal-detail-dq",
    "期望": ".new-resume-personal-expect",
    "底栏": ".card-bottom",
    "沟通按钮": ".card-bottom button, .card-bottom-right button",
}

# 从整卡文本里识别技能标签：全大写或首字母大写的技术词
_SKILL_RE = re.compile(
    r"\b(JAVA|Python|GO|PHP|C\+\+|C#|JS|JavaScript|TypeScript|VUE|React|Android|IOS|"
    r"ORACLE|MySQL|Redis|MongoDB|Hibernate|MyBatis|Spring|SpringBoot|SpringCloud|"
    r"Kafka|Docker|Kubernetes|Linux|Spark|Hadoop|Flink|算法|架构|微服务)\b",
    re.I,
)
# 猎聘的时间格式有两种：(8年4个月) 和 (2年)，都要认
_EXP_LINE_RE = re.compile(
    r"(\d{4}\.\d{2})\s*-\s*(至今|\d{4}\.\d{2})\((?:\d+年)(?:\d+个月)?\)"
)


def _t(scope: Any, sel: str) -> str:
    try:
        el = scope.query_selector(sel)
        if el is None:
            return ""
        return (el.inner_text() or "").replace("\n", " ").strip()
    except Exception:
        return ""


def parse_card_text(raw: str) -> dict[str, Any]:
    """从整卡文本里补出选择器抓不到的东西。

    **关键**：inner_text 会把「公司/学校名」和「时间段」拆成两行，
    所以不能按单行判断，必须"时间段 + 它上面那一行"配对。

        上海翰萨智能科技有限公司 · 移动端研发经理
        2024.09-2026.07(1年10个月)
        黄山学院 · 教育与语言 · 本科 · 统招
        2010.09-2012.06(2年)
    """
    lines = [x.strip() for x in (raw or "").split("\n") if x.strip()]
    skills: list[str] = []
    works: list[str] = []
    edus: list[str] = []
    for i, ln in enumerate(lines):
        for m in _SKILL_RE.finditer(ln):
            w = m.group(1).upper()
            if w not in skills:
                skills.append(w)
        if not _EXP_LINE_RE.search(ln):
            continue
        prev = lines[i - 1] if i > 0 else ""
        entry = f"{prev} {ln}".strip()
        # 判断依据必须是**学历词**，不能是"大学/学院"。
        # 踩过的坑：「清华大学苏州汽车研究院 · 自动驾驶平台开发工程师」
        # 含"大学"但是工作单位，被误判成教育经历，导致筛选结果里混进一堆假清北。
        if any(k in prev for k in ("本科", "硕士", "博士", "大专", "中专", "统招", "非统招", "MBA", "EMBA")):
            edus.append(entry)
        else:
            works.append(entry)
    return {"技能": skills[:8], "工作经历": works[:3], "教育经历": edus[:3]}


def extract_cards(pg: Any, limit: int = 30, with_raw: bool = True) -> list[dict[str, Any]]:
    """提取候选人卡片。只读，不点任何卡片。"""
    cards = pg.query_selector_all(CARD_SEL["容器"])
    print(f"  卡片数：{len(cards)}")
    rows: list[dict[str, Any]] = []
    for c in cards[:limit]:
        try:
            raw = (c.inner_text() or "")
        except Exception:
            raw = ""
        row: dict[str, Any] = {
            "姓名": _t(c, CARD_SEL["姓名"]),
            "年龄": _t(c, CARD_SEL["年龄"]),
            "年限": "" if _t(c, CARD_SEL["工作年限"]) in ("--", "-") else _t(c, CARD_SEL["工作年限"]),
            "学历": _t(c, CARD_SEL["学历"]),
            "地区": _t(c, CARD_SEL["地区"]),
            "期望": re.sub(r"^求职期望[:：]\s*", "", _t(c, CARD_SEL["期望"])),
        }
        for k, v in parse_card_text(raw).items():
            row[k] = v
        if with_raw:
            row["raw"] = re.sub(r"\n{2,}", "\n", raw).strip()[:600]
        rows.append(row)
    return rows


def show_cards(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("  没有卡片")
        return
    banner(f"候选人卡片（{len(rows)} 条）")
    for n, r in enumerate(rows[:6], 1):
        print(f"  {n:>2}. {r['姓名'][:10]:<12}{r['年龄'][:6]:<8}{r['年限'][:10]:<12}"
              f"{r['学历'][:6]:<8}{r['地区'][:8]}")
        if r.get("期望"):
            print(f"      期望：{r['期望'][:50]}")
        if r.get("技能"):
            print(f"      技能：{' / '.join(r['技能'][:6])}")
        if r.get("工作经历"):
            print(f"      最近：{r['工作经历'][0][:60]}")
        if r.get("教育经历"):
            print(f"      学历：{r['教育经历'][0][:60]}")

    banner("字段完整率")
    total = len(rows)
    for k in ("姓名", "年龄", "年限", "学历", "地区", "期望", "技能", "工作经历", "教育经历"):
        hit = sum(1 for r in rows if r.get(k))
        print(f"  {k:<6}{hit:>3}/{total}  {hit / total * 100:>5.0f}%")


def next_page(pg: Any) -> bool:
    """点下一页。猎聘用 Ant Design 分页器。"""
    for sel in (
        "li.ant-pagination-next:not(.ant-pagination-disabled) button",
        "li.ant-pagination-next:not(.ant-pagination-disabled)",
        ".ant-pagination-next:not(.ant-pagination-disabled)",
    ):
        try:
            el = pg.query_selector(sel)
            if el is None:
                continue
            if el.get_attribute("aria-disabled") == "true":
                continue
            el.click(timeout=5000)
            time.sleep(4)
            return True
        except Exception:
            continue
    return False


def collect_pages(pg: Any, max_pages: int = 5) -> list[dict[str, Any]]:
    """翻页采集，按姓名+年龄+经历去重。"""
    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        rows = extract_cards(pg, limit=40, with_raw=False)
        added = 0
        for r in rows:
            key = f"{r.get('姓名','')}|{r.get('年龄','')}|{(r.get('工作经历') or [''])[0][:30]}"
            if key in seen:
                continue
            seen.add(key)
            all_rows.append(r)
            added += 1
        print(f"  第 {page} 页：{len(rows)} 条，新增 {added} 条，累计 {len(all_rows)}")
        if page >= max_pages:
            break
        if not next_page(pg):
            print("  没有下一页了")
            break
    return all_rows


def probe_detail(pg: Any) -> dict[str, Any]:
    """点开第一张候选人卡片，看详情页能看到什么、会不会扣猎币。

    **只点姓名区域，绝不点「立即沟通」**——那个会消耗沟通额度。
    猎币余额在点前点后各读一次，用来判断点详情是否收费。
    """
    def read_coins() -> dict[str, str]:
        js = """() => {
          var out = {};
          var txt = document.body.innerText || '';
          var m = txt.match(/个人猎币\\s*(\\d+)/); if (m) out.personal = m[1];
          var m2 = txt.match(/公司猎币\\s*(\\d+)/); if (m2) out.company = m2[1];
          var m3 = txt.match(/金币\\s*(\\d+)/); if (m3) out.gold = m3[1];
          return out;
        }"""
        try:
            return pg.evaluate(js) or {}
        except Exception:
            return {}

    before = read_coins()
    print(f"  点前余额：{before}")

    cards = pg.query_selector_all(CARD_SEL["容器"])
    if not cards:
        print("✗ 没有卡片可点")
        return {}

    first = cards[0]
    name = _t(first, CARD_SEL["姓名"])
    print(f"  目标：{name}")

    # 点姓名（卡片正文区），不点底栏按钮
    el = first.query_selector(CARD_SEL["姓名"])
    if el is None:
        print("✗ 找不到姓名元素")
        return {}
    try:
        el.click(timeout=6000)
        print("✓ 已点开卡片")
    except Exception as exc:
        print(f"✗ 点击失败：{str(exc)[:70]}")
        return {}
    time.sleep(5)

    # 可能是新标签页，也可能是弹层
    pages = []
    try:
        pages = [x for c in pg.context.pages for x in [c]]
    except Exception:
        pass
    print(f"  当前标签页数：{len(pages)}")
    for x in pages:
        try:
            print(f"    - {x.url[:80]}")
        except Exception:
            pass

    after = read_coins()
    print(f"  点后余额：{after}")
    if before != after:
        print("  ⚠ 余额有变化，点详情可能收费！")
    else:
        print("  ✓ 余额无变化")

    # 详情可能在当前页（弹层/右侧面板），读它的文本
    info: dict[str, Any] = {"name": name, "before": before, "after": after}
    try:
        body = pg.inner_text("body")
        info["body_len"] = len(body)
        # 找详情容器候选
        for sel in (".resume-detail", ".detail-resume", "[class*='resume-detail']",
                    ".ant-drawer-content", ".ant-modal-content"):
            els = pg.query_selector_all(sel)
            if els:
                t = (els[0].inner_text() or "").replace("\n", " | ")
                print(f"\n  详情容器 {sel}（{len(els)} 个）：")
                print(f"    {t[:500]}")
                info["container"] = sel
                info["text"] = t[:1500]
                break
    except Exception as exc:
        print(f"  读取详情失败：{str(exc)[:70]}")

    return info


def dump(pg: Any, tag: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png = OUT_DIR / f"{stamp}_{tag}.png"
    html = OUT_DIR / f"{stamp}_{tag}.html"
    try:
        pg.screenshot(path=str(png))
    except Exception as exc:
        print(f"  截图失败：{exc}")
    try:
        html.write_text(pg.content(), encoding="utf-8")
    except Exception as exc:
        print(f"  HTML 保存失败：{exc}")
    print(f"  截图：{png}")
    print(f"  快照：{html}")
    return html


def main() -> int:
    _console()
    ap = argparse.ArgumentParser(description="猎聘猎头端结构探针")
    ap.add_argument("--inspect", action="store_true", help="只看页面输入框与按钮")
    ap.add_argument("--search", default="", help="执行一次搜索")
    ap.add_argument("--own-profile", action="store_true", help="用独立 profile 启动，不连 liepin-cli")
    ap.add_argument("--detail", action="store_true",
                    help="点开第一张卡片看详情（不点立即沟通）")
    ap.add_argument("--query2", default="", help="配合 --detail：先搜索什么关键词")
    ap.add_argument("--pages", type=int, default=0, help="翻页采集 N 页（只读，不消耗额度）")
    ap.add_argument("--posted-job", default="", help="在发布岗位筛选（按城市/职位名关键词匹配 chip）")
    ap.add_argument("--keep-open", action="store_true", help="结束后不关闭浏览器")
    args = ap.parse_args()

    try:
        from patchright.sync_api import sync_playwright
    except Exception:
        print("✗ 未安装 patchright")
        return 1

    banner("猎聘猎头端探针")
    pw = sync_playwright().start()
    ctx = None
    owned = False
    try:
        ctx, pg, owned = connect(pw, args.own_profile, args.keep_open)

        print(f"\n→ 打开 {URL_SEARCH}")
        try:
            pg.goto(URL_SEARCH, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"  加载异常：{str(exc)[:80]}")
        time.sleep(5)

        cur = pg.url
        title = pg.title()
        print(f"  当前：{cur[:70]}\n  标题：{title[:40]}")
        if "liepin" not in cur:
            print("✗ 页面被重定向，可能登录失效")

        if args.inspect or not args.search:
            banner("页面元素：输入框")
            info = inspect(pg)
            for x in info.get("inputs", []):
                print(f"  <{x['tag']}> type={x['type']:<8} cls={x['cls'][:38]:<40} ph={x['ph'][:34]}")
            banner("页面元素：按钮")
            for x in info.get("buttons", [])[:20]:
                print(f"  <{x['tag']}> cls={x['cls'][:40]:<42} 「{x['text'][:16]}」")

            banner("结构嗅探：可能的列表容器")
            for c in find_container(pg)[:10]:
                print(f"  [{c['count']:>3}] {c['parentCls'][:30]:<32} > {c['childCls'][:26]:<28}")
                print(f"        {c['sample'][:66]}")

            dump(pg, "inspect")

        if args.detail and args.query2:
            banner(f"先搜索：{args.query2}")
            do_search(pg, args.query2)

        if args.search:
            banner(f"执行搜索：{args.search}")
            do_search(pg, args.search)
            print(f"  搜索后 URL：{pg.url[:80]}")

            # 在发布岗位筛选：点 chip 后结果只出该岗位候选人（客户要求的核心条件）
            if args.posted_job:
                chip = pg.evaluate("""(kw) => {
                  var xp = document.evaluate("//*[normalize-space(text())='正在发布的职位：']",
                                             document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                  var label = xp.singleNodeValue;
                  if (!label) return null;
                  var box = label.parentElement;
                  var all = box.querySelectorAll('a, span, div, em');
                  for (var j = 0; j < all.length; j++) {
                    var t = String(all[j].innerText||'').replace(/\\s+/g,' ').trim();
                    if (t.length > 15 && t.indexOf('|') >= 0 && all[j].children.length <= 3) {
                      if (t.indexOf(kw) < 0) continue;
                      var r = all[j].getBoundingClientRect();
                      if (r.width < 40 || r.y < 100) continue;
                      return {x: r.x + r.width/2, y: r.y + r.height/2, t: t.slice(0,44)};
                    }
                  }
                  return null;
                }""", args.posted_job)
                if chip:
                    pg.mouse.click(chip["x"], chip["y"])
                    print(f"  已选岗位: {chip['t'][:44]}")
                    time.sleep(6)
                else:
                    print(f"  ⚠ 岗位 chip 没匹配到（{args.posted_job}），结果未按岗位过滤")

            banner("结果页元素")
            info = inspect(pg)
            for x in info.get("inputs", [])[:8]:
                print(f"  input ph={x['ph'][:40]} cls={x['cls'][:36]}")
            for x in info.get("buttons", [])[:12]:
                print(f"  btn  cls={x['cls'][:40]:<42} 「{x['text'][:14]}」")

            banner("候选人卡片提取")
            rows = extract_cards(pg)
            show_cards(rows)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / "cards.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\n  已保存：{OUT_DIR / 'cards.json'}")

            banner("结构嗅探：结果列表")
            for c in find_container(pg)[:12]:
                print(f"  [{c['count']:>3}] {c['parentCls'][:28]:<30} > {c['childCls'][:24]:<26}")
                print(f"        {c['sample'][:68]}")

            dump(pg, "search")

        if args.pages:
            banner(f"翻页采集 {args.pages} 页")
            rows = collect_pages(pg, max_pages=args.pages)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / "collected.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\n  合计 {len(rows)} 条，已保存 {OUT_DIR / 'collected.json'}")
            banner("学校分布")
            from collections import Counter
            schools: list[str] = []
            for r in rows:
                for e in (r.get("教育经历") or []):
                    schools.append(e.split("·")[0].strip())
            for name, cnt in Counter(schools).most_common(20):
                print(f"  {cnt:>3}  {name}")
            banner("目标院校命中")
            target = ("清华", "北京大学", "北大")
            hit = [r for r in rows
                   if any(t in " ".join(r.get("教育经历") or []) for t in target)]
            print(f"  清华/北大：{len(hit)} 人")
            for r in hit[:5]:
                print(f"    {r['姓名']} {r['年龄']} {r['学历']} {r['地区']}")
                for e in (r.get("教育经历") or [])[:2]:
                    print(f"      {e[:60]}")

        if args.detail:
            banner("点开候选人详情（不点沟通按钮）")
            info = probe_detail(pg)
            if info:
                OUT_DIR.mkdir(parents=True, exist_ok=True)
                (OUT_DIR / "detail.json").write_text(
                    json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                dump(pg, "detail")

        if args.keep_open and owned:
            banner("浏览器保持打开，Ctrl+C 结束")
            while True:
                time.sleep(5)

    except KeyboardInterrupt:
        print("\n已中断")
    finally:
        try:
            if owned and not args.keep_open and ctx is not None:
                ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
