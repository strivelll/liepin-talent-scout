"""一键寻访：筛选 → 打分 → 排序 → 沟通 → 索要，原子操作。

**客户定义的完整流程（2026-11-05）**：
用户编辑好话术、岗位筛选条件、沟通数量后，程序直接：
    ① 按岗位筛选搜索候选人
    ② AI 打分
    ③ 排序
    ④ 取前 N 名沟通（带岗位 + 自定义话术）
    ⑤ 自动索要手机/微信
然后返回执行结果。简历采集是独立操作（collect_library），不在此流程里。

**2026-11-05 修复的遗留问题**（正式跑历次都停在「开聊按钮异常」）：
1. 卡片按钮有「立即沟通」和「继续沟通」两种。后者是**已有会话**，
   点了直接进聊天、根本不弹「请选择职位开聊」对话框 —— 旧版一律等弹窗，必然失败。
2. 弹窗按钮只查 button/a，漏了 span/div 承载的「不选择职位开聊」。
3. 搜索页是脱敏名（高\\*\\*），沟通页是真名（高示例），按姓名找会话永远找不到。
   改为：只认「刚建会话置顶第一条」+ 姓氏校验，宁可跳过也不发错人。
4. 卡片字段靠整段 innerText 截取，姓名被「名片简历」污染、地区全空、
   「期望」变成「在线 陈先生 41岁…」。改为按稳定 class 逐字段采集。
5. 任何失败分支都 continue 跳过进度落盘 → 控制台永远显示 running/done:0。
   改为单人处理函数返回 item，主循环统一收尾。

**页面调度（2026-11-05 20:45 按客户要求重构）**：
    ① 只做一次：goto 找人页 → 输入关键词搜索 → 选在发布的岗位 → 得到结果页
    ② 采集时给每张卡片记下 page_no + card_index
    ③ 打分/排序全是内存运算，**结果页 DOM 一动不动**
    ④ Top N 直接按 card_index 在同一批卡片上点「立即沟通」，
       绝不重新 goto / do_search / 选岗位（列表一变就找不到之前的人）
    ⑤ 全部会话建完后，才统一切一次沟通页发话术 / 索要

旧实现每个候选人都会 goto 搜索页 + 重新搜索 + 重新选岗位，再按姓名文本
去结果里找人——客户实测反馈「这个时候就找不到之前的人了」。

用法：
    python -m scripts.auto_hunt --query "AI" --posted-job "杭州" \
        --text "自定义话术…" --limit 5 --pages 1 --what phone,wechat

    --dry：只跑搜索+打分+排序（不沟通），预览名单
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

from app.browser.liepin_guard import RiskGuard  # noqa: E402
from scripts.probe_liepin import do_search  # noqa: E402
# 复用已在真实账号验证过的操作，不在这里维护第二份实现
from scripts.send_liepin import (  # noqa: E402
    click_action_btn,
    click_posted_job_chip,
    pick_greeting_radio,
    send_text_in_chat,
)

CDP_PORT = 53471
URL_SEARCH = "https://h.liepin.com/search/getConditionItem"
URL_IM = "https://h.liepin.com/im/showmsgnewpage"
RESULT_DIR = ROOT / "data" / "hunt"
CARD_SEL = ".detail-resume-card-wrap"
CONV_ITEM = ".im-ui-contact-info"
LABELS = {"phone": "索要手机", "wechat": "索要微信", "resume": "索要简历"}
CONTACT_OK = ("已建会话", "已有会话", "继续沟通(已有会话)")

SYS_PROMPT = "你是资深技术招聘顾问。只输出 JSON，不要任何解释文字。"

SCORE_TEMPLATE = """岗位要求：
{jd}

候选人（猎聘简历卡片上的字段）：
学历：{edu}
年龄：{age}　工作年限：{years}
所在：{city}
求职期望：{expect}
最近经历：{recent}

判断这个人是否值得主动联系，输出 JSON：
{{"score": 0到100的整数, "verdict": "聊"或"观望"或"放弃", "reason": "一句话，30字以内"}}

评分要点：方向是否真的匹配岗位；年限与职级是否对得上；经历质量。
缺失的信息不要编造。"""

BADGE_WORDS = {"名片简历", "在线", "离线", "今天活跃", "刚刚活跃"}
ACTIVE_RE = re.compile(r"^\d+天内活跃$")


def banner(t: str) -> None:
    print(f"\n{'─' * 60}\n{t}\n{'─' * 60}")


PROGRESS_FILE = ROOT / "data" / "hunt" / "progress.json"
PROGRESS: dict[str, Any] = {
    "time": "", "stages": {
        "search": {"status": "pending", "detail": ""},
        "score": {"status": "pending", "done": 0, "total": 0, "rows": []},
        "sort": {"status": "pending", "detail": ""},
        "contact": {"status": "pending", "done": 0, "total": 0, "rows": []},
    }
}


def save_progress() -> None:
    """阶段进度落盘，控制台轮询展示（客户要的「看到过程和阶段性成果」）。"""
    try:
        PROGRESS["time"] = datetime.now().strftime("%H:%M:%S")
        PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS_FILE.write_text(
            json.dumps(PROGRESS, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 纯逻辑（可单测）

def clean_name(raw: Any) -> str:
    """姓名清洗：去掉「名片简历」标签和活跃状态词。

    实测 .new-resume-personal-name 的 innerText 可能是「张**\\n名片简历」，
    旧版按整段文本正则会取出「名片简历」当姓名，给客户的名单直接穿帮。
    """
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not s:
        return ""
    first = s.split(" ")[0]
    if not first or first in BADGE_WORDS or ACTIVE_RE.match(first):
        return ""
    return first


def classify_chat_button(text: Any) -> str | None:
    """卡片沟通按钮分类：新候选人 / 已有会话。"""
    t = re.sub(r"\s+", "", str(text or ""))
    if t == "立即沟通":
        return "new"
    if t in ("继续沟通", "继续聊", "去沟通"):
        return "existing"
    return None


def choose_chat_button(entries: list[dict[str, Any]]) -> tuple[str, int] | None:
    """弹窗按钮决策：返回 (动作, 元素下标)。

    JS 只负责采集 DOM 文案，决策放 Python 便于回归测试。
    元素范围必须包含 span/div —— 旧版只查 button/a 是「开聊按钮异常」主因之一。
    """
    texts = [re.sub(r"\s+", "", str(e.get("text") or "")) for e in (entries or [])]
    for target, kind in (("立即开聊", "chat-now"), ("不选择职位开聊", "no-job")):
        cands = [i for i, t in enumerate(texts) if t == target]
        if not cands:
            continue
        # div[class*=btn] 可能是与按钮同文案的父容器；优先点真正的 BUTTON/A
        real = [i for i in cands
                if str((entries or [])[i].get("tag") or "").upper() in ("BUTTON", "A")]
        return kind, (real[0] if real else cands[0])
    return None


def normalize_card(raw: dict[str, Any]) -> dict[str, Any] | None:
    """把 JS 采集的原始卡片字段归一化；姓名无效的行直接丢弃。"""
    name = clean_name(raw.get("name_raw"))
    if not name:
        return None
    years = str(raw.get("years") or "").strip()
    if years in ("--", "-", "—"):
        years = ""
    btn = ""
    for b in (raw.get("buttons") or []):
        kind = classify_chat_button(b)
        if kind:
            btn = kind
            break
    return {
        "姓名": name,
        "年龄": str(raw.get("age") or "").strip(),
        "年限": years,
        "学历": str(raw.get("edu") or "").strip(),
        "地区": str(raw.get("city") or "").strip(),
        "期望": str(raw.get("expect") or "").strip(),
        "raw": str(raw.get("raw") or ""),
        "button": btn,
    }


def card_key(r: dict[str, Any]) -> str:
    """去重键：脱敏名重复率极高（王** 在 89 人里有 4 个），必须组合字段。"""
    return "|".join((
        r.get("姓名") or "", r.get("年龄") or "",
        r.get("地区") or "", (r.get("期望") or "")[:12],
    ))


def dedup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if not r.get("姓名"):
            continue
        k = card_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def match_tokens(r: dict[str, Any]) -> tuple[str, ...]:
    """卡片定位特征：姓名 + 年龄 + 期望前 8 字，避免同名点错人。"""
    t: list[str] = []
    for v in (r.get("姓名"), r.get("年龄")):
        if v:
            t.append(str(v))
    e = (r.get("期望") or "").strip()[:8]
    if e:
        t.append(e)
    return tuple(t)


def pick_conv_index(names: list[str], name: str) -> int | None:
    """只认置顶会话（刚发过招呼语的必在最上）+ 姓氏校验。

    搜索页是脱敏名、沟通页是真名，无法精确对应。校验不过就返回 None，
    **宁可跳过也不把话术发给错的人**。
    """
    surname = clean_name(name)[:1]
    if not surname or not names:
        return None
    first = re.sub(r"\s+", "", str(names[0] or ""))
    return 0 if first.startswith(surname) else None


def record_contact_progress(progress: dict[str, Any], idx: int,
                            item: dict[str, Any]) -> None:
    """无论成功失败都记一行，控制台不会卡在 running/done:0。"""
    progress["done"] = idx
    progress.setdefault("rows", []).append({
        "姓名": item.get("姓名"),
        "score": item.get("score"),
        "沟通": item.get("沟通"),
        "话术": item.get("话术") or "",
        "索要": item.get("索要") or {},
    })


def finish_contact_progress(progress: dict[str, Any], blocked: bool) -> None:
    progress["status"] = "stopped" if blocked else "done"


def attach_card_index(rows: list[dict[str, Any]], page_no: int = 1) -> None:
    """采集时给每张卡片记页号 + 页内下标。

    客户要求的流程：一次搜索定名单，打分排序后**直接回到同一张卡片**点沟通，
    绝不重新搜索再按姓名找人（列表一变就找不到之前的人了）。
    """
    for i, r in enumerate(rows):
        r["card_index"] = i
        r["page_no"] = page_no


def card_still_matches(text: str, tokens: tuple[str, ...]) -> bool:
    """按索引取到卡片后用文本复核，DOM 变动时不盲点，避免发错人。"""
    t = str(text or "")
    return bool(tokens) and all(x and x in t for x in tokens)


# ---------------------------------------------------------------- 浏览器操作

def extract_cards(pg: Any) -> list[dict[str, Any]]:
    """按稳定 class 逐字段采集（猎聘用 Ant Design，实测 30 条/页）。

    旧版用 `[class*=card]` + 整段 innerText 切片，会重复命中嵌套容器、
    把「名片简历」当姓名、地区永远为空。
    """
    raws = pg.evaluate("""() => {
      const clean = s => String(s||'').replace(/\\s+/g,' ').trim();
      const out = [];
      document.querySelectorAll('.detail-resume-card-wrap').forEach(c => {
        const f = sel => { const n = c.querySelector(sel); return n ? clean(n.innerText) : ''; };
        out.push({
          name_raw: f('.new-resume-personal-name'),
          age: f('.personal-detail-age'),
          years: f('.personal-detail-workyears'),
          edu: f('.personal-detail-edulevel'),
          city: f('.personal-detail-dq'),
          expect: f('.personal-expect-content'),
          raw: clean(c.innerText).slice(0, 400),
          buttons: [...c.querySelectorAll('button')].map(b => clean(b.innerText)),
        });
      });
      return out;
    }""")
    rows = [x for x in (normalize_card(r) for r in (raws or [])) if x]
    return dedup(rows)


def click_chat_now(pg: Any) -> tuple[bool, str]:
    """弹窗里点「立即开聊」/「不选择职位开聊」，失败带弹窗摘要便于定位。"""
    info = pg.evaluate("""() => {
      const m = document.querySelector('.ant-modal');
      if (!m) return {entries: [], modal: '无弹窗'};
      const es = [...m.querySelectorAll('button, a, span, div[class*=btn]')];
      return {
        modal: String(m.innerText||'').replace(/\\s+/g,' ').slice(0, 120),
        entries: es.map(e => ({tag: e.tagName, text: String(e.innerText||'')})),
      };
    }""")
    decision = choose_chat_button(info.get("entries") or [])
    if not decision:
        try:
            pg.keyboard.press("Escape")   # 关掉弹窗，别挡下一个人
        except Exception:
            pass
        return False, f"未找到开聊按钮(弹窗:{str(info.get('modal'))[:60]})"
    kind, idx = decision
    res = pg.evaluate("""(i) => {
      const m = document.querySelector('.ant-modal');
      if (!m) return 'no-modal';
      const es = m.querySelectorAll('button, a, span, div[class*=btn]');
      if (!es[i]) return 'no-el';
      es[i].click();
      return 'ok';
    }""", idx)
    if res != "ok":
        return False, f"点击失败:{res}"
    time.sleep(4)
    return True, "立即开聊" if kind == "chat-now" else "不选择职位开聊"


def click_card_chat(pg: Any, r: dict[str, Any]) -> tuple[str | None, str]:
    """精确定位候选人卡片并点沟通按钮。

    返回 (new/existing, 卡片摘要)。三重匹配 + `.detail-resume-card-wrap`
    （外层唯一容器），避免 [class*=card] 命中内层重复卡片。
    """
    tokens = list(match_tokens(r))
    if not tokens:
        return None, "无定位特征"
    hit = pg.evaluate("""(tokens) => {
      const clean = s => String(s||'').replace(/\\s+/g,' ').trim();
      const cards = [...document.querySelectorAll('.detail-resume-card-wrap')];
      for (let ci = 0; ci < cards.length; ci++) {
        const t = clean(cards[ci].innerText);
        if (!tokens.every(x => t.indexOf(x) >= 0)) continue;
        const bs = [...cards[ci].querySelectorAll('button')];
        for (let bi = 0; bi < bs.length; bi++) {
          const bt = clean(bs[bi].innerText).replace(/\\s/g,'');
          if (bt === '立即沟通' || bt === '继续沟通') {
            return {card: ci, btn: bi, text: bt, head: t.slice(0, 60)};
          }
        }
      }
      return null;
    }""", tokens)
    if not hit:
        return None, "卡片未找到"
    cards = pg.query_selector_all(CARD_SEL)
    if hit["card"] >= len(cards):
        return None, "卡片索引失效"
    buttons = cards[hit["card"]].query_selector_all("button")
    if hit["btn"] >= len(buttons):
        return None, "按钮索引失效"
    try:
        # 卡片按钮必须用 JS click；原生/坐标点击实测 0 请求
        buttons[hit["btn"]].evaluate("el => el.click()")
    except Exception as exc:                      # noqa: BLE001
        return None, f"按钮点击异常:{str(exc)[:30]}"
    return classify_chat_button(hit["text"]), hit["head"]


def locate_and_click(pg: Any, r: dict[str, Any],
                     max_pages: int = 3) -> tuple[str | None, str]:
    """搜索结果排序每天会变，翻 3 页找人；找不到就如实报，不盲发。"""
    for i in range(max_pages):
        kind, detail = click_card_chat(pg, r)
        if kind:
            return kind, detail
        if i == max_pages - 1:
            break
        nxt = pg.query_selector("li.ant-pagination-next:not(.ant-pagination-disabled) button")
        if nxt is None:
            break
        try:
            nxt.click(timeout=3000)
            time.sleep(3)
        except Exception:
            break
    return None, "卡片未找到(前3页)"


def click_card_by_index(pg: Any, r: dict[str, Any]) -> tuple[str | None, str]:
    """按采集时记录的索引，在**同一搜索结果页**点沟通按钮。

    不 goto、不重新搜索、不重新选岗位——客户要求的核心流程：
    一次搜索定名单，打分排序后直接回到同一批卡片。
    """
    idx = int(r.get("card_index", -1))
    if idx < 0:
        return None, "无卡片索引"
    cards = pg.query_selector_all(CARD_SEL)
    if idx >= len(cards):
        return None, f"卡片索引越界({idx}/{len(cards)})"
    card = cards[idx]
    try:
        text = card.inner_text() or ""
    except Exception:
        return None, "卡片文本读取失败"
    if not card_still_matches(text, match_tokens(r)):
        return None, "卡片文本已变化（跳过，不盲点）"
    for b in card.query_selector_all("button"):
        try:
            kind = classify_chat_button(b.inner_text() or "")
        except Exception:
            continue
        if kind:
            b.evaluate("el => el.click()")
            return kind, re.sub(r"\s+", " ", text)[:60]
    return None, "无沟通按钮"


def goto_result_page(pg: Any, page_no: int) -> bool:
    """同一次搜索内切到第 page_no 页（不重新搜索、不改筛选条件）。"""
    if page_no <= 1:
        return True
    el = (pg.query_selector(f"li.ant-pagination-item-{page_no} a")
          or pg.query_selector(f"li.ant-pagination-item-{page_no}"))
    if el is None:
        return False
    try:
        el.click(timeout=4000)
        time.sleep(3)
        return True
    except Exception:
        return False


def reset_result_page(pg: Any, pages: int) -> None:
    """采集翻页后回到第 1 页，保证沟通阶段的索引从同一页开始。"""
    for _ in range(max(0, pages - 1)):
        prev = pg.query_selector("li.ant-pagination-prev:not(.ant-pagination-disabled) button")
        if prev is None:
            break
        try:
            prev.click(timeout=4000)
            time.sleep(2)
        except Exception:
            break


def open_conv_for(pg: Any, name: str, used: set[int]) -> tuple[bool, str]:
    """在沟通页打开「姓氏匹配且未处理过」的会话，避免发错人。"""
    items = pg.query_selector_all(CONV_ITEM)
    for i, it in enumerate(items[:12]):
        if i in used:
            continue
        try:
            txt = re.sub(r"\s+", " ", (it.inner_text() or "").strip())
        except Exception:
            continue
        if pick_conv_index([txt], name) is None:
            continue
        try:
            it.scroll_into_view_if_needed(timeout=2500)
            time.sleep(0.5)
            box = it.bounding_box()
            if not box:
                continue
            vh = (pg.viewport_size or {}).get("height", 900)
            if not (0 < box["y"] < vh - 80):
                continue
            pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            used.add(i)
            time.sleep(3)
            return True, txt[:16]
        except Exception:
            continue
    return False, "会话未确认(姓氏不匹配)"


def follow_up_all(pg: Any, guard: Any, args: argparse.Namespace,
                  results: list[dict[str, Any]], whats: list[str]) -> None:
    """所有会话建完后，统一切一次沟通页发话术/索要。

    放在末尾而不是每人循环里，结果页在整个「立即沟通」阶段始终不刷新。
    """
    if not (args.text or whats):
        return
    targets = [x for x in results if x.get("沟通") in CONTACT_OK]
    if not targets:
        return
    banner(f"⑤ 统一发送话术 / 索要（{len(targets)} 人）")
    try:
        pg.goto(URL_IM, wait_until="domcontentloaded", timeout=20000)
        guard.after_nav()
        time.sleep(2)
    except Exception as exc:                      # noqa: BLE001
        for x in targets:
            x["话术"] = f"跳过发送:沟通页导航异常({str(exc)[:20]})"
        return
    used: set[int] = set()
    for x in targets:
        ok, why = open_conv_for(pg, x["姓名"], used)
        if not ok:
            x["话术"] = f"跳过发送:{why}"
            x["索要"]["_skip"] = why
            print(f"    {x['姓名']}: ✗ {why}")
            continue
        if args.text:
            sent, msg = send_text_in_chat(pg, args.text)
            x["话术"] = msg if sent else f"失败:{msg}"
            print(f"    {x['姓名']} 话术: {'✓' if sent else '✗'}({x['话术']})")
            time.sleep(random.uniform(1.5, 3))
        for w in whats:
            x["索要"][w] = click_action_btn(pg, LABELS[w])
            print(f"    {x['姓名']} {LABELS[w]}: {x['索要'][w]}")
            time.sleep(random.uniform(1.5, 2.5))


def ensure_conv_open(pg: Any, name: str, guard: Any) -> tuple[bool, str]:
    """确保聊天输入框可见，且确认会话是刚建/刚点的那个。

    点「继续沟通」可能已经直接打开会话；否则去沟通页取置顶第一条并校验姓氏。
    """
    url = pg.url or ""
    if "/im/" in url:
        try:
            ta = pg.query_selector("textarea.ant-im-input")
            if ta and ta.is_visible():
                return True, "当前会话"
        except Exception:
            pass
    try:
        pg.goto(URL_IM, wait_until="domcontentloaded", timeout=20000)
        guard.after_nav()
    except Exception as exc:                      # noqa: BLE001
        return False, f"沟通页导航异常:{str(exc)[:24]}"
    time.sleep(2)
    items = pg.query_selector_all(CONV_ITEM)
    names: list[str] = []
    for it in items[:5]:
        try:
            names.append(re.sub(r"\s+", " ", (it.inner_text() or "").strip())[:20])
        except Exception:
            names.append("")
    idx = pick_conv_index(names, name)
    if idx is None:
        return False, f"会话未确认(置顶:{names[0] if names else '无'})"
    try:
        it = items[idx]
        it.scroll_into_view_if_needed(timeout=2500)
        time.sleep(0.5)
        box = it.bounding_box()
        if not box:
            return False, "会话无坐标"
        vh = (pg.viewport_size or {}).get("height", 900)
        if not (0 < box["y"] < vh - 80):
            return False, "会话不在视口"
        pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        time.sleep(3)
        return True, "置顶会话(姓氏校验通过)"
    except Exception as exc:                      # noqa: BLE001
        return False, f"会话打开异常:{str(exc)[:24]}"


def score_one(llm: Any, jd: str, r: dict[str, Any]) -> dict[str, Any]:
    """单个候选人打分，返回 {score, verdict, reason}。失败降 0 分。"""
    try:
        prompt = SCORE_TEMPLATE.format(
            jd=jd[:300],
            edu=r.get("学历") or "未标注",
            age=r.get("年龄") or "未标注",
            years=r.get("年限") or "未标注",
            city=r.get("地区") or "未标注",
            expect=(r.get("期望") or "未标注")[:40],
            recent=(r.get("raw") or "")[:100],
        )
        resp = llm._chat(SYS_PROMPT, prompt)
        data = json.loads(re.search(r"\{.*\}", resp.text, re.S).group(0))
        return {"score": int(data.get("score", 0) or 0),
                "verdict": str(data.get("verdict", ""))[:6],
                "reason": str(data.get("reason", ""))[:40]}
    except Exception as exc:                      # noqa: BLE001
        return {"score": 0, "verdict": "", "reason": f"打分失败:{str(exc)[:30]}"}


def contact_one(pg: Any, guard: Any, args: argparse.Namespace,
                r: dict[str, Any], whats: list[str]) -> dict[str, Any]:
    """在**当前搜索结果页**按采集索引给单个候选人点「立即沟通」。

    客户要求的流程：一次搜索 → 打分 → Top N 直接在同一批卡片上沟通。
    这里绝不 goto / do_search / 重新选岗位，否则结果列表一变就找不到之前的人。
    自定义话术与索要放到 `follow_up_all` 在末尾统一处理，同样不打断结果页。
    """
    item: dict[str, Any] = {**r, "沟通": "", "话术": "", "索要": {}}

    # 多页采集时切回该候选人所在的那一页（同一次搜索内翻页，不是重新搜索）
    page_no = int(r.get("page_no") or 1)
    if page_no != 1 and not goto_result_page(pg, page_no):
        item["沟通"] = "结果页切换失败"
        return item

    kind, detail = click_card_by_index(pg, r)
    if not kind:
        item["沟通"] = detail
        return item
    if kind == "existing":
        item["沟通"] = "继续沟通(已有会话)"
    else:
        for _ in range(10):
            if pg.evaluate("() => !!document.querySelector('.ant-modal')"):
                break
            time.sleep(1)
        else:
            item["沟通"] = "弹窗未出现"
            return item
        time.sleep(2)
        if args.greet:
            pick_greeting_radio(pg, args.greet)
        ok, diag = click_chat_now(pg)
        if not ok:
            item["沟通"] = f"开聊失败:{diag}"
            return item
        item["沟通"] = "已建会话"
    time.sleep(3)
    return item


def make_parser() -> argparse.ArgumentParser:
    """CLI 契约单独暴露，便于测试前端 build_argv 的参数是否都能被接受。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="AI", help="候选人搜索关键词")
    ap.add_argument("--posted-job", default="", help="在发布岗位筛选关键词（城市/职位名）")
    ap.add_argument("--jd", default="", help="打分用岗位要求；空 = 用岗位信息自动拼装")
    ap.add_argument("--text", default="", help="自定义话术（会话建立后发送）")
    ap.add_argument("--greet", default="", help="打招呼语关键词（弹窗话术列表匹配）")
    ap.add_argument("--pages", type=int, default=1, help="搜索翻页数")
    ap.add_argument("--limit", type=int, default=5, help="沟通数量（取打分前 N）")
    ap.add_argument("--what", default="phone,wechat")
    ap.add_argument("--dry", action="store_true", help="只跑搜索+打分+排序，不沟通")
    return ap


def main() -> int:
    args = make_parser().parse_args()
    whats = [x.strip() for x in args.what.split(",")
             if x.strip() in ("phone", "wechat", "resume")]

    from patchright.sync_api import sync_playwright

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results: list[dict[str, Any]] = []
    scored = 0
    PROGRESS["time"] = stamp
    save_progress()

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        ctx = b.contexts[0]
        pg = next((x for x in ctx.pages if "h.liepin.com" in (x.url or "")), ctx.new_page())

        pg.set_default_timeout(15000)   # 页面导航中 evaluate/wait 会无限挂起（实测卡死 31 分钟）
        guard = RiskGuard(pg, max_actions=60)
        if guard.check():
            print(f"✗ 风控状态（{guard.tripped}）")
            return 1

        # ---------- ① 搜索（岗位筛选） ----------
        banner(f"① 搜索候选人：{args.query}" + (f"（岗位：{args.posted_job}）" if args.posted_job else ""))
        pg.goto(URL_SEARCH, wait_until="domcontentloaded", timeout=45000)
        guard.after_nav()
        if not do_search(pg, args.query):
            print("✗ 搜索失败")
            return 1
        if args.posted_job:
            ok, t = click_posted_job_chip(pg, args.posted_job)
            print(f"  岗位筛选: {'✓ ' + t[:40] if ok else '✗ 未匹配到，全量结果'}")
            if guard.check():
                print(f"✗ 触发风控（{guard.tripped}）")
                return 1

        # 采集（翻页）
        all_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page_no in range(1, args.pages + 1):
            if guard.blocked:
                break
            if not guard.before_action(f"采集第{page_no}页"):
                break
            rows = extract_cards(pg)
            # 记下页号 + 页内下标：打分排序后仍要回到同一张卡片
            attach_card_index(rows, page_no)
            new = 0
            for r in rows:
                k = card_key(r)
                if k in seen:
                    continue
                seen.add(k)
                all_rows.append(r)
                new += 1
            guard.after_action()
            print(f"  第 {page_no} 页：{len(rows)} 条，新增 {new}，累计 {len(all_rows)}")
            if page_no < args.pages:
                nxt = pg.query_selector("li.ant-pagination-next:not(.ant-pagination-disabled) button, "
                                        "li.ant-pagination-next:not(.ant-pagination-disabled)")
                if nxt is None:
                    print("  没有下一页了")
                    break
                try:
                    nxt.click(timeout=4000)
                    time.sleep(4)
                except Exception:
                    break

        PROGRESS["stages"]["search"] = {"status": "done", "detail": f"{len(all_rows)} 人"}
        save_progress()
        if not all_rows:
            print("✗ 没有采集到候选人")
            PROGRESS["stages"]["search"] = {"status": "failed", "detail": "0 人"}
            save_progress()
            return 1

        # ---------- ② AI 打分 ----------
        banner(f"② AI 打分（{len(all_rows)} 人）")
        jd = args.jd.strip() or (
            f"{args.query} 相关岗位" + (f"（{args.posted_job}）" if args.posted_job else "") +
            "。要求具备相关工作经验，方向匹配。")

        # .env 加载（auto_hunt 独立运行时没有 console 帮它注入环境变量——踩过）
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

        from app.llm import llm
        llm.reload()
        if not llm.available:
            print("✗ 模型不可用（设置里配 Key）")
            return 1

        PROGRESS["stages"]["score"] = {"status": "running", "done": 0,
                                       "total": len(all_rows), "rows": []}
        save_progress()
        for i, r in enumerate(all_rows, 1):
            s = score_one(llm, jd, r)
            r.update(s)
            scored = i
            PROGRESS["stages"]["score"]["done"] = i
            PROGRESS["stages"]["score"]["rows"].append({
                "姓名": r["姓名"], "年龄": r.get("年龄"), "年限": r.get("年限"),
                "学历": r.get("学历"), "地区": r.get("地区"),
                "期望": (r.get("期望") or "")[:30],
                "score": s["score"], "verdict": s["verdict"], "reason": s["reason"],
            })
            save_progress()
            print(f"  [{i}/{len(all_rows)}] {r['姓名']:<8}{s['score']:>3} "
                  f"{'█' * max(1, s['score'] // 10)} {s['verdict']}  {s['reason']}")
            time.sleep(random.uniform(0.3, 0.8))
        PROGRESS["stages"]["score"]["status"] = "done"
        save_progress()

        # ---------- ③ 排序 ----------
        all_rows.sort(key=lambda x: -x.get("score", 0))
        todo = all_rows[: args.limit] if args.limit else all_rows
        PROGRESS["stages"]["sort"] = {"status": "done", "detail": f"前 {len(todo)} 名"}
        save_progress()
        banner(f"③ 排序完成，沟通前 {len(todo)} 名")
        for i, r in enumerate(todo, 1):
            print(f"  {i}. {r['score']:>3} {r['姓名']:<8}{r['年限']:<10}{r['期望'][:24]}")

        if args.dry:
            PROGRESS["stages"]["contact"] = {"status": "skipped", "done": 0,
                                             "total": len(todo), "rows": []}
            save_progress()
            banner("dry 模式结束（未沟通）")
            results = [{**r, "沟通": "dry", "索要": {}} for r in todo]
        else:
            # ---------- ④ 逐个沟通 + 索要（统一收尾，任何失败都记进度） ----------
            contact = {"status": "running", "done": 0, "total": len(todo), "rows": []}
            PROGRESS["stages"]["contact"] = contact
            save_progress()
            banner(f"④ 沟通 {len(todo)} 人（留在同一搜索结果页，不重新搜索）")
            if args.pages > 1:
                reset_result_page(pg, args.pages)   # 采集翻页后回到第 1 页，索引才对得上
            for i, r in enumerate(todo, 1):
                if guard.blocked:
                    print(f"\n⚠ 停止：{guard.summary()}")
                    break
                if not guard.before_action(f"{i}/{len(todo)}"):
                    break
                print(f"\n  [{i}/{len(todo)}] {r['姓名']}  分{r['score']}")
                item = contact_one(pg, guard, args, r, whats)
                guard.after_action()
                results.append(item)
                record_contact_progress(contact, i, item)
                save_progress()
                print(f"    沟通: {item['沟通']}")
                if i < len(todo):
                    w = random.uniform(4, 8)
                    print(f"    等待 {w:.0f}s")
                    time.sleep(w)
            finish_contact_progress(contact, guard.blocked)
            save_progress()
            # ⑤ 全部会话建完后才统一切一次沟通页，结果页在 ④ 阶段始终不刷新
            follow_up_all(pg, guard, args, results, whats)
            for pr, done_item in zip(contact.get("rows", []), results):
                pr["话术"] = done_item.get("话术") or ""
                pr["索要"] = done_item.get("索要") or {}
            save_progress()

    # ---------- ⑤ 结果 ----------
    banner("执行结果")
    okn = sum(1 for r in results if r.get("沟通") in CONTACT_OK)
    print(f"  打分 {scored} 人 | 沟通成功 {okn}/{len(results) if not args.dry else 'dry'}")
    for r in results:
        zy = ",".join(f"{k}:{v}" for k, v in (r.get("索要") or {}).items()) or "-"
        print(f"  {r.get('score','—'):>3} {r.get('姓名','?'):<8} {r.get('沟通','')} "
              f"话术:{r.get('话术','-')}  索要[{zy}]")

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULT_DIR / f"hunt_{stamp}.json"
    out.write_text(json.dumps({
        "time": stamp, "query": args.query, "posted_job": args.posted_job,
        "jd": jd[:100], "dry": args.dry, "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  结果文件：{out}")
    print(f"  {guard.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
