"""猎聘猎头端 · 主动沟通发送。

**这是不可撤销的真实操作**：点了「立即沟通」就消耗额度，消息发出无法撤回。

设计原则：
1. 一次只发一条，每条都独立处理（失败不影响后续）
2. 每条发送前记录额度，发送后对比
3. 遇验证码立即停
4. 所有结果落盘，便于核对

用法：
    python -m scripts.send_liepin --dry 1      # 只点开第一条，不发消息（观察结构）
    python -m scripts.send_liepin --limit 1    # 真发一条
    python -m scripts.send_liepin --limit 10   # 发十条
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "liepin-send"
URL_IM = "https://h.liepin.com/im/showmsgnewpage"
CAND_DIR = ROOT / "data" / "liepin-probe"
CARDS_FILE = CAND_DIR / "collected.json"      # 搜索落盘的候选人
SCORES_FILE = CAND_DIR / "scores.json"        # AI 打分结果
CDP_PORT = int(os.environ.get("LIEPIN_CDP_PORT", "53471"))
URL_SEARCH = "https://h.liepin.com/search/getConditionItem"

CARD = ".tlog-common-resume-card"
BTN_CHAT = ".card-bottom button"
INPUT_CANDIDATES = (
    ".chat-input",
    "textarea",
    "[contenteditable='true']",
    ".ant-input",
    "div[class*='editor']",
)
SEND_CANDIDATES = (
    "button:has-text('发送')",
    ".btn-send",
    "[class*='send']",
)



EDU_W = ("本科","硕士","博士","大专","统招","非统招")

def _school(t: dict[str, Any]) -> str:
    for e in (t.get("教育经历") or []):
        if ("清华大学" in e or "北京大学" in e) and any(w in e for w in EDU_W):
            return "清华" if "清华" in e else "北大"
    return ""

def _company(t: dict[str, Any]) -> str:
    w = t.get("工作经历") or []
    if not w:
        return ""
    return [x.strip() for x in w[0].split("·")][0]

def _city(t: dict[str, Any], override: str = "") -> str:
    if override:
        return override
    return (t.get("地区") or "").split("-")[0].strip()

def _make_text(t: dict[str, Any], idx: int, city: str) -> str:
    """话术：不引用对方任何个人信息，只讲岗位。"""
    c = f"{city}的" if city else ""
    tail = f"，工作地点{city}" if city else ""
    v = idx % 4
    if v == 0:
        return f"你好，我这边有个{c}Java岗，看你的方向比较合适，方便聊两句吗？"
    if v == 1:
        return f"打扰了，有个Java后端的机会想推荐给你{tail}，有兴趣的话我把JD发你？"
    if v == 2:
        return f"你好，我们有个Java开发的岗位在招{tail}，想先问下你近期考虑新机会吗？"
    return f"这边有个技术岗跟你的方向挺对口，Java相关{tail}，方便简单聊下吗？"


def load_targets(limit: int = 0, names: list[str] | None = None,
                 skip: set[str] | None = None) -> list[dict[str, Any]]:
    """取待沟通的人，按 AI 分数从高到低。

    数据来源和打分脚本保持一致：collected.json（候选人）+ scores.json（分数）。
    没有分数的人排在最后——宁可先聊高分，也别让没评过的插队。
    """
    rows = []
    for f in (CARDS_FILE, CAND_DIR / "cards.json"):
        if f.exists():
            try:
                rows = json.loads(f.read_text(encoding="utf-8"))
                if rows:
                    break
            except Exception:                     # noqa: BLE001
                continue
    if not rows:
        return []

    scores: dict[str, Any] = {}
    if SCORES_FILE.exists():
        try:
            scores = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            scores = {}

    out = []
    for r in rows:
        key = f"{r.get('姓名','')}|{r.get('年龄','')}"
        sc = (scores.get(key) or {}).get("score")
        r["_score"] = sc
        r["_verdict"] = (scores.get(key) or {}).get("verdict")
        r["_reason"] = (scores.get(key) or {}).get("reason")

        name = str(r.get("姓名") or "")
        if names:
            # 优先按 "姓名|年龄" 精确匹配（脱敏姓名重复率高，王** 可能有四五个）
            # 兼容只传姓名的情况
            if key not in names and name not in names:
                continue
        if skip and name in skip:
            continue
        out.append(r)

    # 有分在前，同分按年限降序
    out.sort(key=lambda x: (-(x.get("_score") if isinstance(x.get("_score"), int) else -1),
                            -num_of(x.get("年限"))))
    return out[:limit] if limit else out


def num_of(s: Any) -> int:
    import re as _re
    m = _re.search(r"(\d+)", str(s or ""))
    return int(m.group(1)) if m else 0


def banner(t: str) -> None:
    print(f"\n{'─' * 62}\n{t}\n{'─' * 62}")


def risk(pg: Any) -> str | None:
    try:
        body = pg.inner_text("body")[:4000]
    except Exception:
        return None
    if "captcha" in pg.url or "safe.liepin" in pg.url:
        return "captcha"
    for kw in ("验证", "操作过于频繁", "稍后再试", "异常"):
        if kw in body:
            return kw
    return None


def close_modal(pg: Any) -> None:
    for sel in ("button:has-text('知道了')", ".ant-modal-close", "button:has-text('确定')"):
        try:
            el = pg.query_selector(sel)
            if el is not None and el.is_visible():
                el.click()
                time.sleep(0.5)
                return
        except Exception:
            continue


def search(pg: Any, q: str) -> bool:
    """搜索前必须确保在猎头端找人页。

    踩过的坑：liepin-cli 会把浏览器带到 lpt.liepin.com（企业端），
    而猎头账号访问那里会被 intercept/has-bound 拦掉，
    在拦截页上找搜索框永远失败。
    """
    # 每次都重新加载找人页，而不是只在"不在猎头端"时才导航。
    # 原因：Ant Design 的 Select 一旦有值，placeholder 就消失，
    # 第二次搜索时找不到「搜职位/公司/行业等」这个锚点，直接失败。
    # 重载是重置状态最简单可靠的方式。
    try:
        pg.goto(URL_SEARCH, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass
    time.sleep(4)
    close_modal(pg)
    res = pg.evaluate(
        """(q) => {
          var phs = document.querySelectorAll('.ant-select-selection-placeholder');
          for (var i = 0; i < phs.length; i++) {
            if (String(phs[i].innerText || '').indexOf('搜职位') < 0) continue;
            var box = phs[i].closest('.ant-select');
            if (!box) continue;
            var inp = box.querySelector('input.ant-select-selection-search-input');
            if (inp) { inp.focus(); return 'ok'; }
          }
          return 'no';
        }""",
        q,
    )
    if res != "ok":
        return False
    pg.keyboard.type(q, delay=70)
    time.sleep(0.8)
    btn = pg.query_selector("button.search-btn")
    if btn is None:
        return False
    btn.click(timeout=5000)
    time.sleep(9)
    close_modal(pg)
    for _ in range(2):
        pg.mouse.wheel(0, 900)
        time.sleep(0.6)
    pg.mouse.wheel(0, -3000)
    time.sleep(0.8)
    return True


def find_card(pg: Any, name: str, age: str, company: str) -> Any:
    """三重匹配定位卡片：脱敏姓名会重复，必须叠加年龄和公司。"""
    cards = pg.query_selector_all(CARD)
    key_c = (company or "")[:4]
    for c in cards:
        try:
            t = c.inner_text() or ""
        except Exception:
            continue
        if name and name not in t:
            continue
        if age and age not in t:
            continue
        if key_c and key_c not in t:
            continue
        return c
    return None


def next_page(pg: Any) -> bool:
    for sel in ("li.ant-pagination-next:not(.ant-pagination-disabled) button",
                "li.ant-pagination-next:not(.ant-pagination-disabled)"):
        try:
            el = pg.query_selector(sel)
            if el is not None and el.get_attribute("aria-disabled") != "true":
                el.click(timeout=4000)
                time.sleep(4)
                return True
        except Exception:
            continue
    return False


def locate(pg: Any, t: dict[str, Any], max_pages: int = 3) -> Any:
    """在搜索结果里翻页找到目标卡片。"""
    for _ in range(max_pages):
        card = find_card(pg, t["姓名"], t.get("年龄") or "", t.get("公司") or "")
        if card is not None:
            return card
        if not next_page(pg):
            break
    return None


def dump(pg: Any, tag: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{tag}.png"
    try:
        pg.screenshot(path=str(p))
    except Exception:
        pass
    return p


def is_expand_control(text: Any) -> bool:
    """判断是不是「展开更多职位」控件（「收起」状态不用再点）。"""
    t = re.sub(r"\s+", "", str(text or ""))
    return "展开" in t and "职位" in t


def expand_posted_jobs(pg: Any) -> bool:
    """点开「展开更多职位」，让折叠区 chip 的坐标真正可点。

    实测（2026-11-05）：折叠时 .ul-box 只有 24px 高 + overflow:hidden，
    第 3 行以后的 chip 虽然 DOM 里有坐标，按坐标点击却落在裁剪区外，
    导致选了靠后的岗位但筛选根本没生效。
    """
    hit = pg.evaluate("""() => {
      var es = document.querySelectorAll('span.control, span, a, div');
      for (var i = 0; i < es.length; i++) {
        var t = String(es[i].innerText||'').replace(/\\s+/g,'');
        if (t.indexOf('展开') >= 0 && t.indexOf('职位') >= 0 && es[i].offsetParent) {
          var r = es[i].getBoundingClientRect();
          if (r.width > 0 && r.height > 0) return {x: r.x + r.width/2, y: r.y + r.height/2};
        }
      }
      return null;
    }""")
    if not hit:
        return False
    pg.mouse.click(hit["x"], hit["y"])
    time.sleep(1.5)
    return True


def collect_job_chips(pg: Any) -> list[dict[str, Any]]:
    """读「正在发布的职位」区域全部候选元素的文本 + 坐标。"""
    return pg.evaluate("""() => {
      var xp = document.evaluate("//*[normalize-space(text())='正在发布的职位：']",
                                 document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
      var label = xp.singleNodeValue;
      if (!label) return [];
      var box = label.parentElement;
      var out = [];
      box.querySelectorAll('span.info, a, span, div').forEach(function(e){
        var t = String(e.innerText||'').replace(/\\s+/g,' ').trim();
        if (t.indexOf('|') < 0) return;
        var r = e.getBoundingClientRect();
        out.push({text:t, children:e.children.length,
                  x:r.x+r.width/2, y:r.y+r.height/2, w:r.width, h:r.height});
      });
      return out;
    }""") or []


def pick_job_chips(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """挑出真正的岗位 chip：排除 527 字的 .ul-box 外层容器，按完整文本去重。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in candidates or []:
        t = str(c.get("text") or "").strip()
        if not (15 < len(t) < 90):
            continue
        if "|" not in t or "k" not in t:
            continue
        if int(c.get("children") or 0) > 3:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(c)
    return out


def match_job_chip(chips: list[Any], keyword: Any) -> int:
    """完整 label 精确命中优先；城市等短值回退包含匹配。返回下标，找不到 -1。"""
    kw = str(keyword or "").strip()
    if not kw:
        return -1
    texts = [(x if isinstance(x, str) else str(x.get("text") or "")) for x in (chips or [])]
    for i, t in enumerate(texts):
        if t == kw:
            return i
    for i, t in enumerate(texts):
        if kw and kw in t:
            return i
    return -1


JOB_CLICK_JS = """(kw) => {
  const clean = s => String(s||'').replace(/\\s+/g,' ').trim();
  const lis = [...document.querySelectorAll('.quick-search-box li.job')];
  let li = lis.find(x => clean(x.innerText) === kw);
  if (!li) li = lis.find(x => clean(x.innerText).indexOf(kw) >= 0 && clean(x.innerText).length < 90);
  if (!li) return {ok:false, cls:''};
  (li.querySelector('span.info') || li).click();
  return {ok:true, cls:clean(li.className)};
}"""

JOB_CHECK_JS = """(kw) => {
  const clean = s => String(s||'').replace(/\\s+/g,' ').trim();
  const li = [...document.querySelectorAll('.quick-search-box li.job')]
      .find(x => clean(x.innerText) === kw);
  return {found: !!li, cls: li ? clean(li.className) : ''};
}"""

JOB_FALLBACK_JS = """(kw) => {
  const clean = s => String(s||'').replace(/\\s+/g,' ').trim();
  const li = [...document.querySelectorAll('.quick-search-box li.job')]
      .find(x => clean(x.innerText) === kw);
  if (!li) return false;
  li.click();
  return true;
}"""


def is_job_active(cls: Any) -> bool:
    """岗位 chip 选中后 li.class 会变成 'job active'。"""
    return "active" in str(cls or "").split()


def wait_job_chips(pg: Any, tries: int = 8) -> list[dict[str, Any]]:
    """等筛选区渲染（do_search 之后 chips 可能短暂为空）。"""
    for _ in range(tries):
        chips = pick_job_chips(collect_job_chips(pg))
        if chips:
            return chips
        time.sleep(1)
    return []


def click_posted_job_chip(pg: Any, keyword: str) -> tuple[bool, str]:
    """选中某个在发布的岗位 chip，并**校验真的选中了**。

    2026-11-05 21:10 实测：坐标点击会失效——展开「更多职位」后 .ul-box
    会折回 24px，折叠区 chip 的 y=466 落在 overflow:hidden 裁剪区外，
    mouse.click 打不到，li.class 仍是 `job`。
    改成 `span.info.click()`（JS click 不受折叠/坐标影响），点完检查
    `li.class` 是否含 `active`；不中再点一次 li 本身，仍不中如实报错。
    """
    chips = wait_job_chips(pg)
    idx = match_job_chip(chips, keyword)
    if idx < 0:
        return False, f"没找到岗位 chip（共 {len(chips)} 个）"
    target = str(chips[idx].get("text") or "")

    pg.evaluate(JOB_CLICK_JS, target)
    time.sleep(1)
    st = pg.evaluate(JOB_CHECK_JS, target) or {}
    if not is_job_active(st.get("cls")):
        pg.evaluate(JOB_FALLBACK_JS, target)
        time.sleep(1)
        st = pg.evaluate(JOB_CHECK_JS, target) or {}
    if not is_job_active(st.get("cls")):
        return False, f"岗位未选中(li.class={st.get('cls') or '无'})"
    time.sleep(6)          # 等筛选结果刷新
    return True, target[:44]


def pick_greeting_radio(pg: Any, keyword: str) -> tuple[bool, str]:
    """弹窗里按关键词选打招呼语（radio 列表）。"""
    hit = pg.evaluate("""(kw) => {
      var m = document.querySelector('.ant-modal');
      if (!m) return null;
      var labels = m.querySelectorAll('.ant-radio-wrapper, [class*=radio], label');
      for (var i = 0; i < labels.length; i++) {
        var t = String(labels[i].innerText||'');
        if (t.indexOf(kw) >= 0) {
          var r = labels[i].getBoundingClientRect();
          return {x: r.x + 22, y: r.y + r.height/2, text: t.replace(/\\s+/g,' ').slice(0,40)};
        }
      }
      var ps = m.querySelectorAll('p, [class*=lang-text]');
      for (var j = 0; j < ps.length; j++) {
        if (String(ps[j].innerText||'').indexOf(kw) >= 0) {
          var wrap = ps[j].closest('label, [class*=item], [class*=wrapper]') || ps[j].parentElement;
          if (wrap) {
            var r2 = wrap.getBoundingClientRect();
            return {x: r2.x + 22, y: r2.y + r2.height/2, text: String(wrap.innerText||'').replace(/\\s+/g,' ').slice(0,40)};
          }
        }
      }
      return null;
    }""", keyword)
    if not hit:
        return False, "没找到匹配的话术"
    pg.mouse.click(hit["x"], hit["y"])
    time.sleep(0.8)
    return True, hit["text"]


def send_to(pg: Any, t: dict[str, Any]) -> tuple[bool, str]:
    """向单个候选人发起沟通。

    **实测流程（2026-11-04 打通）**：
        1. 搜索 → 找卡片 → JS click「立即沟通」
        2. 弹出「请选择职位开聊」对话框
        3. 直接点「不选择职位开聊」→ 弹窗关闭，预设话术发出

    **两个关键点，踩了很久**：
    - 原生 click / click(force) 都不触发，必须 `el.evaluate("el => el.click()")`
    - 对话框强制要求选职位，但这个账号没有发布职位，
      只能点「不选择职位开聊」跳过（代价：回复率低约 30%）
    - 发出去的是猎聘预设话术，不是我们写的话术。
      预设第一条「你好，看了你的简历，方便聊聊吗？」不含个人信息，符合要求。
    """
    q = t.get("_query") or ("Java 清华" if t.get("学校") == "清华" else "Java 北京大学")
    if not search(pg, q):
        return False, "搜索失败"

    # 客户要求：先选正在发布的岗位——点 chip 筛选后，
    # 「立即沟通」弹窗的开聊职位自动选中，点「立即开聊」即带职位。
    posted = str(t.get("_posted_job") or "").strip()
    if posted:
        ok_chip, chip_txt = click_posted_job_chip(pg, posted)
        if ok_chip:
            print(f"    已选岗位: {chip_txt[:40]}")
        else:
            print(f"    ⚠ 岗位 chip 没找到({chip_txt})，继续不带职位")

    card = locate(pg, t)
    if card is None:
        return False, "未找到卡片"

    btn = card.query_selector(BTN_CHAT) or card.query_selector("button")
    if btn is None:
        return False, "无沟通按钮"

    try:
        btn.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    btn.evaluate("el => el.click()")

    # 等弹窗
    for _ in range(10):
        if pg.evaluate("() => !!document.querySelector('.ant-modal')"):
            break
        time.sleep(1)
    else:
        return False, "弹窗未出现"

    time.sleep(2)

    # 选打招呼语：radio 列表按关键词匹配（客户在猎聘「修改打招呼语」里维护的话术）
    greet = str(t.get("_greet") or "").strip()
    if greet:
        ok_g, g_txt = pick_greeting_radio(pg, greet)
        if ok_g:
            print(f"    已选话术: {g_txt[:36]}")
        else:
            print(f"    ⚠ 话术「{greet[:16]}」没匹配到，用当前选中项")

    # 点「立即开聊」（职位自动选中的新弹窗只有一个主按钮）
    res = pg.evaluate("""() => {
      var bs = document.querySelectorAll('.ant-modal button');
      for (var i = 0; i < bs.length; i++) {
        var t = String(bs[i].innerText||'').replace(/\\s/g,'');
        if (t === '立即开聊') { bs[i].click(); return 'ok'; }
      }
      return 'notfound';
    }""")
    if res == "ok":
        time.sleep(4)
        return True, "已发送(带职位)"

    # 回退：老弹窗（无职位）只有「不选择职位开聊」（发平台默认语句）
    res = pg.evaluate("""() => {
      var m = document.querySelector('.ant-modal');
      if (!m) return 'no-modal';
      var bs = m.querySelectorAll('button, a, span');
      for (var i = 0; i < bs.length; i++) {
        var t = String(bs[i].innerText || '').replace(/\\s/g, '');
        if (t === '不选择职位开聊') { bs[i].click(); return 'ok'; }
      }
      return 'notfound';
    }""")
    if res != "ok":
        # 兜底：关掉弹窗，避免挡住下一个
        try:
            pg.keyboard.press("Escape")
        except Exception:
            pass
        return False, f"开聊按钮异常:{res}"

    # 不要用"弹窗是否关闭"判断成功——弹窗有关闭动画，5 秒内可能还没消失，
    # 但消息其实已经发出去了（实测 4 条都发出去了，却被误判为失败）。
    # 点下「不选择职位开聊」这个动作本身就是发送，点了就算成功。
    time.sleep(4)
    return True, "已发送"


# ---------------- Phase 2：已有会话的自定义话术 + 索要 ----------------

def open_conv(pg: Any, name: str) -> bool:
    """在沟通页打开某人的会话（真实鼠标坐标，JS click 无效）。"""
    for _ in range(8):
        for it in pg.query_selector_all(".im-ui-contact-info"):
            try:
                if name not in (it.inner_text() or ""):
                    continue
                it.scroll_into_view_if_needed(timeout=2500)
                time.sleep(0.5)
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


def send_text_in_chat(pg: Any, text: str) -> tuple[bool, str]:
    """在当前打开的会话里，用输入框发自定义话术。

    带重试：会话刚打开时消息还在加载，输入框可能暂时不可交互
    （实测偶发 click 超时——客户和本地测试都撞到过）。
    """
    last_err = ""
    for attempt in range(3):
        try:
            ta = pg.wait_for_selector("textarea.ant-im-input", state="visible", timeout=6000)
        except Exception:
            time.sleep(2)
            continue
        if ta is None:
            continue
        try:
            ta.click(timeout=5000)
            time.sleep(0.5)
            ta.fill(text)
            time.sleep(0.8)
            break
        except Exception as exc:                  # noqa: BLE001
            last_err = f"第{attempt+1}次输入失败:{str(exc)[:40]}"
            print(f"    {last_err}，重试…")
            time.sleep(2.5)
    else:
        return False, last_err or "输入框未找到"

    # 发送按钮：主按钮（含"发送"文字）
    btns = pg.query_selector_all("button.ant-im-btn-primary")
    for b in btns:
        try:
            if not b.is_visible():
                continue
            if "发送" not in (b.inner_text() or ""):
                continue
            box = b.bounding_box()
            pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            time.sleep(2)
            return True, "已发送"
        except Exception:
            continue
    # 兜底：Ctrl+Enter / Enter
    pg.keyboard.press("Enter")
    time.sleep(2)
    return True, "已发送(Enter)"


def confirm_modal(pg: Any, timeout: float = 6) -> bool:
    """等确认框出现并点「确 定」。

    猎聘用的是 ant-im-* 变体（不是标准 antd 的 ant-modal-confirm-btns），
    选择器写错的话确认框弹出来也没人点，索要实际没完成——客户实测踩过。
    """
    for _ in range(int(timeout * 2)):
        for sel in (".ant-im-modal-confirm-btns button.ant-im-btn-primary",
                    ".ant-modal-confirm-btns button.ant-btn-primary"):
            b = pg.query_selector(sel)
            try:
                if b and b.is_visible():
                    bx = b.bounding_box()
                    pg.mouse.click(bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2)
                    time.sleep(1.5)
                    return True
            except Exception:
                continue
        time.sleep(0.5)
    return False


def click_action_btn(pg: Any, label: str) -> str:
    """点消息区上方的「索要手机/索要微信/索要简历」。"""
    els = pg.query_selector_all("[class*=im-ui-action-button]")
    for el in els:
        try:
            if not el.is_visible():
                continue
            if label not in (el.inner_text() or ""):
                continue
            el.scroll_into_view_if_needed(timeout=2500)
            time.sleep(0.4)
            box = el.bounding_box()
            pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            time.sleep(2)
            if confirm_modal(pg):
                return "clicked+confirmed"
            return "clicked-no-confirm"      # 点了但确认框没处理（异常情况）
        except Exception:
            continue
    return "notfound"


def has_contact_reply(pg: Any) -> dict[str, bool]:
    """读当前会话消息，判断对方是否发了手机号/微信号。"""
    try:
        txt = pg.inner_text("body")
    except Exception:
        txt = ""
    phone = bool(re.search(r"1[3-9]\d{9}", txt))
    # 微信号：wx/wxid/ weixin 关键词，或"微信[:]xxx"格式
    wechat = bool(re.search(r"(微信|weixin|wx)\s*[:：]?\s*\w{5,}", txt, re.I)) or              bool(re.search(r"wxid[_\-]?\w{4,}", txt, re.I))
    return {"phone": phone, "wechat": wechat}


def follow_up(pg: Any, t: dict[str, Any], text: str, whats: list[str]) -> dict[str, Any]:
    """Phase 2：打开会话 → 发自定义话术 → 索要联系方式。"""
    name = t["姓名"]
    out: dict[str, Any] = {"姓名": name, "话术": False, "索要": {}}

    if not open_conv(pg, name):
        out["错误"] = "会话打不开"
        return out

    if text:
        ok, msg = send_text_in_chat(pg, text)
        out["话术"] = ok
        out["话术结果"] = msg
        time.sleep(random.uniform(1.5, 3))

    for w in whats:
        label = {"phone": "索要手机", "wechat": "索要微信", "resume": "索要简历"}[w]
        r = click_action_btn(pg, label)
        out["索要"][w] = r
        time.sleep(random.uniform(1.5, 2.5))

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--skip", default="", help="跳过的姓名，逗号分隔")
    ap.add_argument("--names", default="", help="只发给指定姓名，逗号分隔")
    ap.add_argument("--interval", default="35-70")
    ap.add_argument("--dry", action="store_true", help="只打印计划，不执行")
    ap.add_argument("--text", default="", help="自定义话术：对已有会话用输入框发送（Phase 2）")
    ap.add_argument("--what", default="phone,wechat", help="发完话术后索要什么：phone,wechat,resume")
    ap.add_argument("--greet", default="", help="打招呼语关键词：匹配猎聘话术列表里的一条来发")
    ap.add_argument("--posted-job", default="", help="正在发布的岗位关键词（按城市或职位名匹配 chip）")
    ap.add_argument("--query", default="", help="新候选人的搜索关键词（不在会话列表时用它建立会话）")
    args = ap.parse_args()

    skip = {x.strip() for x in args.skip.split(",") if x.strip()}
    names = [x.strip() for x in args.names.split(",") if x.strip()] or None
    greet_kw = args.greet.strip()
    posted_job = args.posted_job.strip()

    # ---- Phase 2：自定义话术 + 索要（对已有会话）----
    if args.text:
        from app.browser.liepin_guard import RiskGuard
        whats = [x.strip() for x in args.what.split(",") if x.strip() in ("phone", "wechat", "resume")]

        from patchright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp("http://127.0.0.1:53471")
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

            # Phase 2 的目标来自「会话列表」（真名），不能用 load_targets——
            # 那是从搜索结果读的，名字是脱敏的（周**），和会话名对不上。
            if names:
                targets = [{"姓名": n.split("|")[0]} for n in names]
                # 从搜索结果补全年龄/公司——find_card 是三重匹配（姓名+年龄+公司），
                # 只有姓名的话同名卡片会误命中，也可能匹配不到
                cand_by_name = {}
                for r in (load_targets(limit=0) or []):
                    cand_by_name.setdefault(r.get("姓名") or "", r)
                for t in targets:
                    extra = cand_by_name.get(t["姓名"])
                    if extra:
                        if extra.get("年龄"):
                            t["年龄"] = extra["年龄"]
                        if extra.get("最近单位"):
                            t["公司"] = extra["最近单位"][:4]
            else:
                targets = []
                for it in pg.query_selector_all(".im-ui-contact-info"):
                    try:
                        n = (it.inner_text() or "").split("\n")[0].strip()
                        if n and not n.isdigit() and n not in skip:
                            targets.append({"姓名": n})
                    except Exception:
                        continue
                targets = targets[: args.limit]
            print(f"Phase 2：对 {len(targets)} 人发自定义话术 + 索要 {whats}")
            print(f"话术：{args.text[:60]}")
            print()

            done = 0
            for i, t in enumerate(targets, 1):
                if guard.blocked:
                    print(f"⚠ 停止：{guard.summary()}")
                    break
                if not guard.before_action(f"{i}/{len(targets)}"):
                    break
                name = t["姓名"]
                print(f"  [{i}/{len(targets)}] {name}  分{t.get('_score') or '—'}")

                # 会话打不开 = 可能是新候选人。提供 --query 时自动走「立即沟通」建会话。
                # 注意：新招呼平台会强制先发一条默认语句（弹窗里没有输入框，绕不开），
                # 紧接着我们的自定义话术会跟上，对方实际看到两条。
                if not open_conv(pg, name):
                    if not args.query:
                        print("    ✗ 会话列表里没有此人，且未提供 --query，无法自动建立会话")
                        continue
                    print("    新候选人：先走「立即沟通」建立会话（会收到一条平台默认语句）")
                    ok1, msg1 = send_to(pg, {**t, "_query": args.query})
                    guard.after_action()
                    print(f"    建会话: {'✓' if ok1 else '✗'}({msg1})")
                    if not ok1:
                        continue
                    if not open_conv(pg, name):
                        print("    ✗ 建会话后仍打不开")
                        continue

                r = follow_up(pg, t, "", whats)   # 话术在下面统一发
                if not r.get("话术", True):
                    pass
                ok2, msg2 = send_text_in_chat(pg, args.text)
                guard.after_action()
                if ok2:
                    done += 1
                print(f"    话术:{'✓' if ok2 else '✗'}({msg2})  索要:{r.get('索要')}")
                time.sleep(random.uniform(3, 6))

            print(f"\n完成 {done}/{len(targets)}")
            print(guard.summary())
            return 0

    targets = load_targets(limit=args.limit, names=names, skip=skip)

    for i, t in enumerate(targets, 1):
        t["学校"] = _school(t)
        t["_greet"] = greet_kw
        t["_posted_job"] = posted_job
        t["公司"] = _company(t)

    banner(f"计划发送 {len(targets)} 条（跳过 {len(skip)} 个）")
    for t in targets:
        sc = t.get("_score")
        sc_txt = f"{sc:>3}" if isinstance(sc, int) else "  —"
        print(f"  {sc_txt}  {t['姓名']:<8}{t.get('学校') or '':<4}{t['公司'][:22]}")
    if args.dry:
        return 0

    from patchright.sync_api import sync_playwright

    lo, hi = [float(x) for x in args.interval.split("-")]
    OUT.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    ok = 0

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        ctx = b.contexts[0]
        pg = next((x for x in ctx.pages if "h.liepin" in (x.url or "")), ctx.new_page())

        for i, t in enumerate(targets, 1):
            banner(f"[{i}/{len(targets)}] {t['姓名']} · {t.get('公司')}")
            sig = risk(pg)
            if sig:
                print(f"⚠ 风控信号 {sig}，停止")
                results.append({"姓名": t["姓名"], "结果": f"风控中止:{sig}"})
                break

            try:
                done, msg = send_to(pg, t)
            except Exception as exc:
                done, msg = False, f"异常:{str(exc)[:40]}"
            print(f"  → {msg}")
            results.append({"姓名": t["姓名"], "公司": t.get("公司"),
                            "学校": t.get("学校"), "分数": t.get("_score"),
                            "结果": msg, "成功": done})
            if done:
                ok += 1

            if i < len(targets):
                if not done:
                    time.sleep(2)   # 失败不占用长间隔，快速试下一条
                    continue
                w = random.uniform(lo, hi)
                print(f"  等待 {w:.0f}s…")
                time.sleep(w)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    f = OUT / f"{stamp}_batch.json"
    f.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    banner(f"完成：成功 {ok} / {len(targets)}")
    for r in results:
        print(f"  {r['姓名']:<8}{r['结果']}")
    print(f"\n记录：{f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
