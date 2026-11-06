"""从猎聘「简历管理」页批量导出完整简历（含头像）。

**这是比遍历聊天更好的路径**：
- 聊天会话只有 27 个，这里能看到 96 份（客户在猎聘上收到的全部简历）
- 详情页信息完整：头像、联系方式、求职意向、工作经历——正是客户要的形态
- 每份详情页自带编号（res_id_encode），天然的去重键

**产物**：
    data/resumes/{姓名}_{res_id前8位}/
      resume.pdf     完整简历（含头像，page.pdf 导出）
      resume.txt     纯文本
      resume.json    元数据（res_id、URL、时间）

**风控纪律**：96 份一口气点完必然触发验证码（上午的教训）。
所以：单次上限、随机间隔 4-8 秒、每 3 个体检一次。96 份建议分 4-6 批跑完。

用法：
    python -m scripts.collect_library --scan                # 只列出，不导出
    python -m scripts.collect_library --limit 10            # 导出前 10 份
    python -m scripts.collect_library --limit 10 --offset 10 # 从第 11 份开始
"""

from __future__ import annotations

import argparse
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

from app.browser.liepin_guard import RiskGuard  # noqa: E402

CDP_PORT = 53471
URL_LIST = "https://h.liepin.com/resumemanage/showmytalentlist"
OUT_ROOT = ROOT / "data" / "resumes" / "library_html"   # 客户要求：统一文件夹，纯 HTML 文件
INDEX_FILE = ROOT / "data" / "resumes" / "library_index.json"


def banner(t: str) -> None:
    print(f"\n{'─' * 60}\n{t}\n{'─' * 60}")


def wait_ready(pg: Any, min_len: int = 400, tries: int = 8) -> bool:
    """等详情页内容加载完（正文 > min_len 字）。"""
    for _ in range(tries):
        try:
            if len(pg.inner_text("body")) >= min_len:
                return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def scrape_list(pg: Any, pages: int = 12) -> list[dict[str, Any]]:
    """抓简历列表：姓名 + res_id + 详情 URL。自动翻页。"""
    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    time.sleep(4)      # 等列表渲染。不等的话第一页 innerText 全是空（实测踩过）

    for page_no in range(1, pages + 1):
        rows = pg.evaluate(
            """() => {
              var out = [];
              // 从行出发找链接，而不是从链接出发找行。
              // 踩过的坑：链接元素本身没有文本（姓名在行的其他单元格里），
              // 从 a 出发时 a.innerText 是空的，row.closest 也可能命中空容器，
              // 结果 40 条 URL 全部没有姓名和行文本。
              document.querySelectorAll('tr, li, [class*=item]').forEach(function(e){
                if (!e.offsetParent) return;
                var a = e.querySelector('a[href*="res_id_encode"]');
                if (!a) return;
                var t = String(e.innerText||'').replace(/\\s+/g,' ').trim();
                if (t.length < 10) return;
                var m = t.match(/^([\\u4e00-\\u9fa5]{2,4})\\s/);
                out.push({
                  name: m ? m[1] : '',
                  url: String(a.href||''),
                  row: t.slice(0,120)
                });
              });
              return out;
            }"""
        )
        added = 0
        for r in rows or []:
            url = r.get("url") or ""
            m = re.search(r"res_id_encode=([A-Za-z0-9]+)", url)
            if not m:
                continue
            rid = m.group(1)
            if rid in seen:
                continue
            seen.add(rid)
            all_rows.append({"name": r.get("name") or "", "res_id": rid, "url": url,
                             "row_text": r.get("row") or ""})
            added += 1
        print(f"  第 {page_no} 页：{len(rows or [])} 条，新增 {added}，累计 {len(all_rows)}")

        # 翻页：找「下一页」按钮
        nxt = pg.query_selector("li.ant-pagination-next:not(.ant-pagination-disabled) button, "
                                "li.ant-pagination-next:not(.ant-pagination-disabled)")
        if nxt is None:
            print("  没有下一页了")
            break
        try:
            nxt.click(timeout=4000)
            time.sleep(4)      # 翻页后同样要等渲染
        except Exception:
            print("  翻页失败，停止")
            break

    return all_rows


EMBED_JS = """async () => {
  // 把页面里的图片全部转成 base64 内嵌，这样存成单个 HTML 离线也能看头像
  const imgs = document.querySelectorAll('img');
  let ok = 0;
  for (const img of imgs) {
    try {
      if (!img.src || img.src.startsWith('data:')) continue;
      const resp = await fetch(img.src);
      const blob = await resp.blob();
      const dataUrl = await new Promise(res => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.readAsDataURL(blob);
      });
      img.src = dataUrl;
      ok++;
    } catch (e) { /* 防盗链或加载失败的跳过 */ }
  }
  return ok;
}"""


def export_one(pg: Any, row: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """打开详情页 → 图片内嵌的 HTML + 文本。返回结果。

    按客户要求（2026-11-04）：格式用 HTML 不用 PDF（PDF 入库慢），
    且每个简历是单个 HTML 文件、放在统一文件夹里，方便批量导入自己的库。
    """
    pg.goto(row["url"], wait_until="domcontentloaded", timeout=45000)

    if not wait_ready(pg, min_len=400, tries=8):
        return {"ok": False, "err": "加载超时"}

    # 图片内嵌 base64（头像等），存完离线可看
    embedded = pg.evaluate(EMBED_JS)
    time.sleep(1)

    text = pg.inner_text("body")

    # 姓名优先从详情页取（列表里的可能带脱敏）
    name = row.get("name") or ""
    m = re.search(r"([\u4e00-\u9fa5]{2,4})(?:\s*[①?!]|\s*$)", text[:200])
    if not name:
        name = m.group(1) if m else row["res_id"][:8]

    # 完整 HTML（备用）+ PDF（客户最终选择：HTML 垃圾数据多，PDF 干净）
    full_html = pg.content()
    (out_dir / "resume_full.html").write_text(full_html, encoding="utf-8")

    pdf_ok = False
    try:
        pg.pdf(path=str(out_dir / "resume.pdf"), format="A4",
               margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"},
               print_background=True)
        pdf_ok = True
    except Exception as exc:                      # noqa: BLE001
        print(f"      PDF 失败：{str(exc)[:50]}")

    (out_dir / "resume.txt").write_text(text, encoding="utf-8")
    (out_dir / "resume.json").write_text(json.dumps({
        "_姓名": name,
        "_res_id": row["res_id"],
        "_url": row["url"],
        "_来源": "猎聘简历管理页",
        "_采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_图片内嵌": embedded,
        "原始文本": text[:3000],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"ok": True, "name": name, "text_len": len(text), "pdf": pdf_ok,
            "dir": str(out_dir), "imgs": embedded}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="只列出清单，不导出")
    ap.add_argument("--limit", type=int, default=0, help="本次最多导出几份（0 = 全部，不建议）")
    ap.add_argument("--offset", type=int, default=0, help="跳过前 N 份（配合分批）")
    ap.add_argument("--list-pages", type=int, default=12, help="列表最多翻几页")
    args = ap.parse_args()

    from patchright.sync_api import sync_playwright

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        ctx = b.contexts[0]
        pg = next((x for x in ctx.pages if "h.liepin.com" in (x.url or "")), ctx.new_page())

        guard = RiskGuard(pg, max_actions=45)   # 列表翻页 + 详情打开都算动作
        if guard.check():
            print(f"✗ 当前在风控状态（{guard.tripped}），先人工验证")
            return 1

        banner("打开简历管理页")
        pg.goto(URL_LIST, wait_until="domcontentloaded", timeout=45000)
        guard.after_nav()
        if guard.check():
            print(f"✗ 触发风控（{guard.tripped}）")
            return 1

        banner("抓取简历列表")
        all_rows = scrape_list(pg, pages=args.list_pages)
        if not all_rows:
            print("✗ 没抓到列表")
            return 1

        # 去掉本地已导出的（按 res_id）
        done: set[str] = set()
        if INDEX_FILE.exists():
            try:
                done = {x.get("res_id") for x in json.loads(INDEX_FILE.read_text(encoding="utf-8"))}
            except Exception:
                done = set()
        todo = [r for r in all_rows if r["res_id"] not in done]
        print(f"\n  库里共 {len(all_rows)} 份，本地已导 {len(done)} 份，待导 {len(todo)} 份")

        if args.offset:
            todo = todo[args.offset:]
        if args.limit:
            todo = todo[: args.limit]

        if args.scan:
            banner(f"清单（{len(todo)} 份）")
            for i, r in enumerate(todo, 1):
                print(f"  {i:>3}. {r['name']:<8} {r['row_text'][:56]}")
            # 存索引供核对
            INDEX_FILE.write_text(json.dumps(
                [{**r, "_exported": False} for r in all_rows], ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"\n  索引：{INDEX_FILE}")
            return 0

        banner(f"开始导出 {len(todo)} 份")
        results = []
        for i, r in enumerate(todo, 1):
            if guard.blocked:
                print(f"\n⚠ 停止：{guard.summary()}")
                break
            if not guard.before_action(f"{i}/{len(todo)}"):
                print(f"\n⚠ 停止：{guard.summary()}")
                break

            name = r.get("name") or r["res_id"][:8]
            print(f"\n  [{i}/{len(todo)}] {name}  {r['row_text'][:46]}")

            res = export_one(pg, r, OUT_ROOT)
            guard.after_action()

            if res.get("ok"):
                results.append({**r, **res})
                print(f"    ✓ {res.get('name')}  {res.get('text_len')} 字  PDF:{'✓' if res.get('pdf') else '✗'}")
            else:
                results.append({**r, "ok": False, "err": res.get("err")})
                print(f"    ✗ {res.get('err')}")

            # 索引及时更新，中断后能接着跑
            INDEX_FILE.write_text(json.dumps(
                [{**x, "_exported": x["res_id"] in {y["res_id"] for y in results if y.get("ok")}}
                 for x in all_rows], ensure_ascii=False, indent=2), encoding="utf-8")

            if i < len(todo):
                w = random.uniform(4, 8)
                print(f"      等待 {w:.0f}s")
                time.sleep(w)

    # 汇总
    ok_list = [r for r in results if r.get("ok")]
    banner(f"完成：成功 {len(ok_list)} / {len(results)}")
    for r in ok_list:
        print(f"  {r.get('name','?'):<10}{r.get('text_len','?')} 字  {Path(r['dir']).name}")
    if guard.tripped:
        print(f"\n⚠ {guard.summary()}")

    # 更新索引
    INDEX_FILE.write_text(json.dumps(
        [{**r, "_exported": r["res_id"] in {y["res_id"] for y in ok_list}} for r in all_rows],
        ensure_ascii=False, indent=2), encoding="utf-8")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (ROOT / "data" / "resumes" / f"library_export_{stamp}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
