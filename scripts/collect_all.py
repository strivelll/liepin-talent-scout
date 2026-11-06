"""批量采集简历：只采「对方已回复联系方式」的会话，采集后自动入人才库。

**客户硬要求（2026-11-05）**：对方回复了手机号/微信，才采集他的简历。
会话列表的最后一条消息预览里，平台会把对方发来的号码替换成 `[手机号]` /
`[微信号]` 标记（实测 30 个会话里 4 个 `[手机号]`、1 个 `微信号`）。
我方发出的「方便回复手机号或微信吗？」属于索要话术，**不算回复**。

**三道闸**：
1. 联系方式筛选（本轮新增，客户要求）
2. 单次上限 + 随机间隔 4-8 秒 + 每 3 个体检一次风控
3. 只采「有简历且本地没有」的

**PDF 头部**：聊天侧栏的 `.im-ui-resume-wrap` 只有求职意向/经历，
没有姓名和头像。本轮从会话项取 `img.im-ui-avatar-img`，在原页面上下文
转成 base64 内嵌，再拼一个信息头，PDF 里就有头像 + 姓名 + 岗位 + 基本信息。

**产物**：data/resumes/{姓名}_{时间}/ 下 resume.pdf / resume.html / resume.txt / resume.json
采集结束后自动调 `app.talent.import_dirs()` 写人才库。

用法：
    python -m scripts.collect_all --scan            # 只列出哪些会话符合，不采集
    python -m scripts.collect_all --limit 5         # 最多采 5 个
    python -m scripts.collect_all --limit 5 --dry   # 演练：点开但不落盘
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import talent as talent_lib  # noqa: E402
from app.browser.liepin_guard import RiskGuard  # noqa: E402

CDP_PORT = 53471
URL_IM = "https://h.liepin.com/im/showmsgnewpage"
RESUME_DIR = ROOT / "data" / "resumes"
CONV_ITEM = ".im-ui-contact-info"
PANEL = ".im-ui-resume-container, .im-ui-resume-wrap"
PRIVACY_MARK = "隐私状态"

# 平台把对方发来的号码替换成方括号标记；裸号 / wxid 也认
CONTACT_TAG_RE = re.compile(r"\[(手机号|微信号|微信|联系方式)\]")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
WECHAT_RE = re.compile(
    r"(?:wxid[_\-]?[A-Za-z0-9]{4,}|(?:微信|weixin|wx)\s*[:：]\s*[A-Za-z0-9_\-]{5,})",
    re.I,
)


def banner(t: str) -> None:
    print(f"\n{'─' * 60}\n{t}\n{'─' * 60}")


# ---------------------------------------------------------------- 纯逻辑（可单测）

def contact_reply(text: Any) -> dict[str, Any]:
    """判断会话预览里是否有「对方回复的联系方式」。

    只认三类硬信号：平台方括号标记、裸手机号、wxid/微信号表达式。
    我方索要话术（「方便回复手机号或微信吗」）不含这些信号，不会误判。
    """
    t = str(text or "")
    tags = list(dict.fromkeys(m.group(0) for m in CONTACT_TAG_RE.finditer(t)))
    phone = "[手机号]" in tags or bool(PHONE_RE.search(t))
    wechat = any(x in tags for x in ("[微信号]", "[微信]", "[联系方式]")) \
        or bool(WECHAT_RE.search(t))
    return {"ok": phone or wechat, "phone": phone, "wechat": wechat, "tags": tags}


def parse_conv_name(raw: Any) -> str:
    """会话条目第一行常是姓名，也可能是未读数。"""
    lines = [x.strip() for x in str(raw or "").split("\n") if x.strip()]
    for ln in lines[:3]:
        if ln.isdigit():
            continue
        ln = ln.replace("[未读]", "").strip()
        if ln and len(ln) <= 12:
            return ln
    return ""


def extract_basics(text: Any) -> dict[str, str]:
    """从侧栏简历文本里恢复能恢复的字段，取不到就留空、不编造。"""
    s = str(text or "")
    lines = [x.strip() for x in s.split("\n") if x.strip()]
    out = {"expect": "", "city": "", "company": "", "edu": ""}
    for label, key in (("求职意向", "expect"), ("工作经历", "company")):
        try:
            i = lines.index(label)
            if i + 1 < len(lines):
                out[key] = lines[i + 1]
        except ValueError:
            pass
    m = re.search(r"·\s*([\u4e00-\u9fa5]{2,8})", s)
    if m:
        out["city"] = m.group(1)
    for ln in lines:
        for k in ("本科", "硕士", "博士", "大专"):
            if k in ln:
                out["edu"] = k
                return out
    return out


def build_header(name: str, title: str, avatar_b64: str,
                 basics: dict[str, str], reply: dict[str, Any]) -> str:
    """PDF 顶部信息条。网页文本一律转义，防 XSS。"""
    esc = lambda v: html.escape(str(v or ""), quote=True)  # noqa: E731
    if avatar_b64:
        avatar = f'<img class="rp-avatar" src="{esc(avatar_b64)}" alt="头像">'
    else:
        avatar = '<div class="rp-avatar rp-noavatar">无头像</div>'
    badge = ('<span class="rp-badge">已回复联系方式</span>' if reply.get("ok")
             else '<span class="rp-badge rp-muted">未回复联系方式</span>')
    tags = " ".join(esc(x) for x in (reply.get("tags") or []))
    kv = [("现职", title), ("求职意向", basics.get("expect")),
          ("城市", basics.get("city")), ("学历", basics.get("edu")),
          ("最近公司", basics.get("company"))]
    rows = "".join(
        f'<div class="rp-kv"><span>{esc(k)}</span><b>{esc(v)}</b></div>'
        for k, v in kv if v)
    return (f'<div class="rp-header">{avatar}<div class="rp-main">'
            f'<div class="rp-title"><b>{esc(name)}</b>{badge}<i>{tags}</i></div>'
            f'<div class="rp-kvs">{rows}</div></div></div>')


HEADER_CSS = """
.rp-header{display:flex;gap:16px;align-items:flex-start;border:1px solid #e5e7eb;
  border-radius:8px;padding:16px;margin-bottom:18px;background:#fafafa}
.rp-avatar{width:88px;height:88px;object-fit:cover;border-radius:6px;flex:none;
  border:1px solid #e5e7eb;background:#fff}
.rp-noavatar{display:flex;align-items:center;justify-content:center;
  font-size:11px;color:#9ca3af}
.rp-main{flex:1;min-width:0}
.rp-title{display:flex;align-items:center;gap:10px;font-size:18px;margin-bottom:8px}
.rp-title i{font-style:normal;font-size:11px;color:#6b7280}
.rp-badge{font-size:11px;padding:2px 8px;border-radius:10px;background:#dcfce7;color:#15803d}
.rp-badge.rp-muted{background:#f3f4f6;color:#6b7280}
.rp-kvs{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:11px;color:#374151}
.rp-kv span{color:#9ca3af;margin-right:5px}
"""


# ---------------------------------------------------------------- 浏览器操作

def local_index() -> set[str]:
    """本地已采集的姓名集合。"""
    names: set[str] = set()
    if not RESUME_DIR.exists():
        return names
    for d in RESUME_DIR.iterdir():
        if not d.is_dir() or not talent_lib.is_resume_dir(d.name):
            continue
        base = d.name.rsplit("_", 2)[0]
        if base:
            names.add(base)
        j = d / "resume.json"
        if j.exists():
            try:
                data = json.loads(j.read_text(encoding="utf-8"))
                if data.get("_姓名"):
                    names.add(str(data["_姓名"]))
            except Exception:                     # noqa: BLE001
                pass
    return names


def conv_name(it: Any) -> str:
    try:
        return parse_conv_name(it.inner_text() or "")
    except Exception:
        return ""


def conv_snapshots(pg: Any) -> list[dict[str, Any]]:
    """一次取回全部会话：姓名、职位、预览文本、头像 URL、联系方式判定。"""
    raw = pg.evaluate("""() => {
      const clean = s => String(s||'').replace(/\\s+/g,' ').trim();
      return [...document.querySelectorAll('.im-ui-contact-info')].map(it => {
        const img = it.querySelector('img.im-ui-avatar-img');
        return {raw: String(it.innerText||''), text: clean(it.innerText),
                avatar: img ? (img.src||'') : ''};
      });
    }""")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in raw or []:
        name = parse_conv_name(s.get("raw"))
        if not name or name in seen:
            continue
        seen.add(name)
        lines = [x.strip() for x in str(s.get("raw") or "").split("\n") if x.strip()]
        raw_title = lines[1] if len(lines) > 1 and len(lines[1]) <= 24 else ""
        title = "" if raw_title == name else raw_title
        out.append({**s, "name": name, "title": title,
                    "reply": contact_reply(s.get("text"))})
    return out


def avatar_mime(data: bytes) -> str:
    """按魔数判图片类型。猎聘头像 URL 是 .png，内容却常是 JPEG。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def fetch_avatar_b64(pg: Any, url: str) -> str:
    """把头像转成可内嵌的 data URL。

    页面内 `fetch(img.src)` 会被 CORS 拦掉（实测返回空），所以走浏览器上下文的
    HTTP 请求：不受 CORS 限制、带 cookie。失败才依次回退到页面内 fetch / 外链 URL。
    """
    if not url:
        return ""
    try:
        resp = pg.context.request.get(url, timeout=12000)
        if resp.ok:
            data = resp.body()
            if data:
                return (f"data:{avatar_mime(data)};base64,"
                        + base64.b64encode(data).decode("ascii"))
    except Exception:                             # noqa: BLE001
        pass
    try:
        inner = str(pg.evaluate("""async (u) => {
          try {
            const r = await fetch(u, {credentials: 'include'});
            const b = await r.blob();
            return await new Promise(res => {
              const f = new FileReader();
              f.onload = () => res(String(f.result || ''));
              f.onerror = () => res('');
              f.readAsDataURL(b);
            });
          } catch (e) { return ''; }
        }""", url) or "")
        if inner.startswith("data:"):
            return inner
    except Exception:                             # noqa: BLE001
        pass
    # 外链兜底：<img src> 跨域加载不受 CORS 限制，PDF 打印时仍能渲染
    return url


def click_conv(pg: Any, name: str) -> bool:
    """点开会话。必须用真实鼠标坐标，JS click 对会话项无效。"""
    for _ in range(10):
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


def has_resume(pg: Any) -> tuple[bool, str]:
    """判断当前会话有没有可读简历，返回 (有, 预览文本)。"""
    panels = pg.query_selector_all(PANEL)
    best = ""
    for p in panels:
        try:
            if not p.is_visible():
                continue
            t = (p.inner_text() or "").strip()
            if len(t) > len(best):
                best = t
        except Exception:
            continue
    if not best:
        return False, ""
    if PRIVACY_MARK in best and len(best) < 40:
        return False, ""
    if len(best) < 80:
        return False, ""
    return True, best[:100].replace("\n", " / ")


def grab(pg: Any) -> tuple[str, str]:
    """抓简历 HTML 与文本。"""
    panels = pg.query_selector_all(PANEL)
    best_el, best_txt = None, ""
    for p in panels:
        try:
            if not p.is_visible():
                continue
            t = p.inner_text() or ""
            if len(t) > len(best_txt):
                best_el, best_txt = p, t
        except Exception:
            continue
    if best_el is None:
        return "", ""
    try:
        return best_el.inner_html(), best_txt
    except Exception:
        return "", best_txt


def to_pdf(pg: Any, html_body: str, out_path: Path,
           header_html: str = "") -> bool:
    """用离屏页生成 PDF；头部信息（含 base64 头像）拼在简历正文前。"""
    page2 = None
    try:
        page2 = pg.context.new_page()
        page2.set_content(
            "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
            'body{font-family:"Microsoft YaHei",sans-serif;font-size:12px;'
            "line-height:1.7;color:#222;padding:24px}"
            "img{max-width:100%}"
            ".im-ui-resume-container,.im-ui-resume-wrap{max-width:100%!important;"
            "height:auto!important;max-height:none!important;overflow:visible!important}"
            + HEADER_CSS +
            "</style></head><body>" + header_html + html_body + "</body></html>",
            wait_until="domcontentloaded", timeout=20000,
        )
        time.sleep(1)
        page2.pdf(path=str(out_path), format="A4",
                  margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
                  print_background=True)
        return True
    except Exception as exc:                      # noqa: BLE001
        print(f"      PDF 失败：{str(exc)[:60]}")
        return False
    finally:
        try:
            if page2 is not None:
                page2.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="只列出符合条件的会话，不采集")
    ap.add_argument("--limit", type=int, default=8, help="本次最多处理几个会话")
    ap.add_argument("--dry", action="store_true", help="点开检查但不落盘")
    ap.add_argument("--all", action="store_true", help="连已采集过的也重新采")
    args = ap.parse_args()

    from patchright.sync_api import sync_playwright

    done = local_index()
    print(f"本地已有简历：{len(done)} 份" + (f"（{', '.join(list(done)[:6])}…）" if done else ""))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stats = {"会话总数": 0, "已回复联系方式": 0, "未回复跳过": 0,
             "有简历": 0, "已采过": 0, "无简历": 0, "采集成功": 0, "失败": 0}

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        ctx = b.contexts[0]
        pg = next((x for x in ctx.pages if "h.liepin.com" in (x.url or "")), None)
        if pg is None:
            print("✗ 找不到猎聘页面")
            return 1

        guard = RiskGuard(pg, max_actions=40)
        if guard.check():
            print(f"✗ 当前已在风控状态（{guard.tripped}），先人工验证")
            return 1

        banner("打开沟通页")
        pg.goto(URL_IM, wait_until="domcontentloaded", timeout=45000)
        guard.after_nav()

        for _ in range(4):
            pg.mouse.wheel(0, 900)
            time.sleep(1.2)

        snaps = conv_snapshots(pg)
        stats["会话总数"] = len(snaps)
        eligible = [s for s in snaps if s["reply"]["ok"]]
        stats["已回复联系方式"] = len(eligible)
        stats["未回复跳过"] = len(snaps) - len(eligible)
        print(f"会话 {len(snaps)} 个 → 已回复联系方式 {len(eligible)} 个，"
              f"未回复跳过 {stats['未回复跳过']} 个")
        print("  符合条件：" + ", ".join(
            f"{s['name']}({'+'.join(s['reply']['tags']) or '号码'})" for s in eligible[:10]))

        if args.scan:
            banner("扫描（不采集）")
            for i, s in enumerate(snaps, 1):
                if i > args.limit:
                    print(f"  …还有 {len(snaps) - args.limit} 个未列（--limit 控制）")
                    break
                mark = "✓已回复" if s["reply"]["ok"] else "—未回复"
                print(f"  [{i}] {s['name']:<12}{mark:<10}{str(s['reply']['tags']):<24}"
                      f"{s['text'][:44]}")
            print(f"\n  只有「已回复联系方式」的会话会被采集。{guard.summary()}")
            return 0

        banner(f"开始采集（上限 {args.limit} 个会话，仅限已回复联系方式）")
        collected = 0
        for i, s in enumerate(eligible, 1):
            if collected >= args.limit:
                print(f"\n  已达上限 {args.limit} 个，停止")
                break
            if guard.blocked:
                print(f"\n  ⚠ 停止：{guard.summary()}")
                break
            n = s["name"]
            if n in done and not args.all:
                stats["已采过"] += 1
                continue
            if not guard.before_action():
                print(f"  ⚠ 停止：{guard.summary()}")
                break

            print(f"\n  [{i}/{len(eligible)}] {n}  联系方式:{','.join(s['reply']['tags']) or '号码'}")
            if not click_conv(pg, n):
                stats["失败"] += 1
                print("    点不开")
                guard.after_action()
                continue
            guard.after_action()

            ok, preview = has_resume(pg)
            if not ok:
                stats["无简历"] += 1
                print("    没有可读简历")
                time.sleep(random.uniform(2, 4))
                continue
            stats["有简历"] += 1
            print(f"    有简历：{preview[:56]}")

            if args.dry:
                print("    （演练模式，不落盘）")
                collected += 1
                time.sleep(random.uniform(3, 6))
                continue

            body_html, txt = grab(pg)
            if len(txt) < 80:
                stats["失败"] += 1
                print("    抓取内容过短")
                continue

            avatar = fetch_avatar_b64(pg, s.get("avatar") or "")
            basics = extract_basics(txt)
            header = build_header(n, s.get("title") or "", avatar, basics, s["reply"])

            out = RESUME_DIR / f"{n}_{stamp}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "resume.html").write_text(
                "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
                + HEADER_CSS + "</style></head><body>" + header + body_html + "</body></html>",
                encoding="utf-8")
            (out / "resume.txt").write_text(txt, encoding="utf-8")
            (out / "resume.json").write_text(json.dumps({
                "_姓名": n,
                "_职位": s.get("title") or "",
                "_采集时间": stamp,
                "_来源": "h.liepin.com/im",
                "_联系方式": "、".join(s["reply"]["tags"]) or "对方回复的号码/微信号",
                "_联系方式已回复": bool(s["reply"]["ok"]),
                "_头像内嵌": avatar.startswith("data:"),
                "_会话预览": s.get("text") or "",
                "原始文本": txt[:2000],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            to_pdf(pg, body_html, out / "resume.pdf", header_html=header)
            print(f"    ✓ 已存 {out.name}（{len(txt)} 字，头像:{'✓' if avatar else '✗'}）")
            stats["采集成功"] += 1
            collected += 1
            time.sleep(random.uniform(4, 8))

    banner("完成")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"  {guard.summary()}")

    if not args.dry and not args.scan:
        try:
            st = talent_lib.import_dirs()
            print(f"\n  人才库同步：新增 {st['新增']}，已存在 {st['已存在']}，跳过 {st['跳过']}")
            print(f"  人才库当前：{talent_lib.stats()}")
        except Exception as exc:                  # noqa: BLE001
            print(f"  ⚠ 人才库同步失败：{str(exc)[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
