"""生成薪资字体的字符映射表。

**什么时候需要跑它**

`app/boss_font.py` 里的映射是 2026-10-21 实测的。如果哪天探测时发现
"未识别字符数 > 0"，说明 BOSS 换了字体，映射过期了，需要重新生成。

**它怎么工作**

不是去下载字体文件解析 cmap——那样还得处理 woff2 解压、glyph 名无语义等问题。
这里用最直接的办法：**让页面自己渲染**。

注入一段 HTML，用页面上 `.job-salary` 的真实 font-family 把 PUA 字符逐个画出来，
截图存盘。打开图就能看到"U+E031 长什么样"，照着填回 `PUA_TO_DIGIT` 即可。

**为什么不用自动识别**

PUA 字符没有语义，glyph 名可能是 `uniE031` 这种废话。自动比对字形需要引入
OCR 或轮廓相似度计算，误差还会引入错误映射——而错误映射比没有映射更危险：
它会让薪资静默算错，筛选条件悄悄失效。人眼看 10 个数字只要 5 秒，不划算去做自动化。

用法：
    python -m scripts.probe_font_map
    python -m scripts.probe_font_map --start E020 --end E05F
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROFILE_DIR = ROOT / "data" / "chrome-profile-geek"
OUT_DIR = ROOT / "data" / "probe-filter"
URL = "https://www.zhipin.com/web/geek/jobs?query=%E8%BF%90%E8%90%A5&city=101250100"

INJECT_JS = """
(args) => {
  const el = document.querySelector('.job-salary');
  if (!el) return {ok: false, reason: 'no .job-salary element'};
  const ff = getComputedStyle(el).fontFamily;

  const old = document.getElementById('__pua_map__');
  if (old) old.remove();

  const box = document.createElement('div');
  box.id = '__pua_map__';
  box.style.cssText = [
    'position:fixed','top:0','left:0','z-index:2147483647',
    'background:#fff','color:#000','padding:14px 18px',
    'border:2px solid #222','font-family:Consolas,monospace',
    'font-size:15px','line-height:1.35','width:520px'
  ].join(';');

  let html = '<div style="font-weight:bold;margin-bottom:6px;font-size:16px">薪资字体映射表</div>';
  html += '<div style="color:#666;margin-bottom:10px;font-size:12px">font-family: ' + ff + '</div>';
  html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">';
  for (let c = args.start; c <= args.end; c++) {
    const hex = c.toString(16).toUpperCase();
    html += '<div style="text-align:center">'
         +  '<div style="font-size:11px;color:#888">U+' + hex + '</div>'
         +  '<div style="font-family:' + ff + ';font-size:34px;line-height:1.1;border:1px solid #ddd;padding:2px 0">'
         +  '&#x' + c.toString(16) + ';</div></div>';
  }
  html += '</div>';
  box.innerHTML = html;
  document.body.appendChild(box);
  return {ok: true, fontFamily: ff};
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="生成薪资字体 PUA 映射表")
    ap.add_argument("--start", default="E030", help="起始码位（十六进制）")
    ap.add_argument("--end", default="E04F", help="结束码位（十六进制）")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--profile", default="")
    args = ap.parse_args()

    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            try:
                if s.isatty():
                    s.reconfigure(encoding="gbk", errors="replace")
            except Exception:
                pass

    start = int(args.start, 16)
    end = int(args.end, 16)

    try:
        from patchright.sync_api import sync_playwright
    except Exception:
        print("✗ 未安装 patchright，请先运行 setup.bat")
        return 1

    profile = Path(args.profile).expanduser() if args.profile else PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_png = OUT_DIR / "pua_map.png"

    print("─" * 66)
    print("薪资字体映射表生成")
    print("─" * 66)
    print(f"码位范围：U+{args.start.upper()} ~ U+{args.end.upper()}")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel="chrome", headless=False, viewport=None,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            print(f"→ 打开 {args.url}")
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                print(f"  加载异常（继续）：{exc}")
            time.sleep(6)

            res = page.evaluate(INJECT_JS, {"start": start, "end": end})
            if not res or not res.get("ok"):
                print(f"✗ 注入失败：{res.get('reason') if res else '未知原因'}")
                print("  可能页面没加载出职位列表，或还没登录。先跑 run_probe.bat 登录一次。")
                return 2

            print(f"  字体：{res.get('fontFamily')}")
            time.sleep(1.5)

            box = page.query_selector("#__pua_map__")
            if box is None:
                print("✗ 找不到注入的表格元素")
                return 2
            box.screenshot(path=str(out_png))
            print(f"✓ 已保存：{out_png}")

            # 顺便把当前页面的薪资样本也打出来，方便对照
            samples = page.evaluate(
                """() => Array.from(document.querySelectorAll('.job-salary')).slice(0,5)
                       .map(e => Array.from(e.textContent||'').map(ch =>
                            ch.codePointAt(0) >= 0xE000 && ch.codePointAt(0) <= 0xF8FF
                              ? '<U+' + ch.codePointAt(0).toString(16).toUpperCase() + '>'
                              : ch).join(''))"""
            )
            print("\n当前页面薪资样本（PUA 字符已标出）：")
            for s in samples:
                print(f"  {s}")

            print("\n" + "─" * 66)
            print("下一步")
            print("─" * 66)
            print("1. 打开上面那张图，看每个 U+XXXX 下面画的是什么数字")
            print("2. 把对应关系填进 app/boss_font.py 的 PUA_TO_DIGIT")
            print("3. 重跑 scripts/probe_filter.py 验证 unknown_chars 是否归零")
            print()
            print("如果图里出现方块/空白，说明该码位在字体里没有字形，跳过即可。")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
