"""真实环境探针：用真实账号做只读体检，不碰业务写操作。

**为什么单独做一个脚本，而不是直接跑主系统**

求职账号和招聘账号是两个完全不同的后台：
- 招聘端 /web/boss/… ：看的是候选人列表，能做筛选、打招呼、约面
- 求职端 /web/geek/… ：看的是职位列表，能做搜索、投递、和 HR 聊

所以用求职账号**测不了招聘业务链路**。但它能测通真实环境里最难、最不确定的部分：

1. Patchright + 系统 Chrome 能不能正常起浏览器
2. 登录态能不能持久化（下次免扫码）
3. 真实页面上会不会立刻弹验证码 / 被判定为机器
4. 选择器机制能不能用（命中率如何）
5. 页面结构长什么样（改版后怎么快速重建选择器）

这五项通过之后，换招聘账号只需要重校准一遍选择器，业务代码不用动。
而且求职账号被封的损失，远小于企业招聘账号。

**只读原则**

默认只做读取和诊断，不发消息、不投递、不点任何会改变账号状态的东西。
整个探测过程对平台来说就是"看页面"，风险最低。

用法：
    python -m scripts.probe_real
    python -m scripts.probe_real --url https://www.zhipin.com/web/geek/job-recommend
    python -m scripts.probe_real --keep-open      # 探测完不关浏览器
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
OUT_DIR = ROOT / "data" / "probe"

# 求职端页面的候选选择器（招聘端的那套在 config/selectors.yaml 里，不能混用）
GEEK_SELECTORS: dict[str, list[str]] = {
    # 以下顺序按 2026-10-21 实测结果排（命中最多、最精确的放最前）
    "职位卡片容器": [
        ".job-card-wrap",
        ".job-card-box",
        ".rec-job-list .card-area",
        "[class*='job-card']",
        "[class*='job-list'] li",
    ],
    "职位标题": [
        ".job-name",
        ".job-title",
        "[class*='job-name']",
        "a[href*='/job_detail/']",
    ],
    "公司名称": [
        # 实测：BOSS 求职端没有 company-name 这个 class，
        # 公司名和地区挤在同一个 .company-location 里，需要按 " · " 或空格拆分
        ".company-location",
        ".job-card-footer .company-location",
        "[class*='company']",
    ],
    "薪资": [
        ".job-salary",
        ".salary",
        "[class*='salary']",
    ],
    "地区": [
        ".company-location",
        "[class*='location']",
        ".job-area",
    ],
    "标签": [
        ".tag-list li",
        "[class*='tag'] span",
        "[class*='label'] span",
    ],
    "HR 信息": [
        ".boss-info",
        ".job-card-footer",
        "[class*='boss-info']",
    ],
    "HR 姓名": [
        ".boss-name",
        "[class*='boss-name']",
    ],
    "登录按钮": [
        ".btn-login",
        "[class*='login-btn']",
        "a[ka*='login']",
    ],
    "用户头像/昵称": [
        ".nav-figure",
        ".user-nav",
        "[class*='user-name']",
    ],
}

# 页面结构嗅探脚本：找出"子元素结构一致"的容器，用来发现列表的真实 class
SNIFF_JS = """
() => {
  const out = [];
  const all = document.querySelectorAll('body *');
  for (const el of all) {
    const kids = el.children;
    const n = kids.length;
    if (n < 4 || n > 80) continue;
    const first = kids[0];
    if (!first) continue;
    // 只取第一个 class token 比较。
    // 原来比整个 className，遇到 "card-area is-seen" 这种带状态类的元素就永远不相等，
    // 导致真正的列表容器被漏掉（实测 .rec-job-list 就被漏了）。
    const keyOf = (el) => {
      const c = el.className;
      if (typeof c !== 'string') return '';
      const parts = c.trim().split(/\\s+/);
      return parts[0] || '';
    };
    const cls = keyOf(first);
    if (!cls) continue;
    let same = 0;
    for (const c of kids) {
      if (keyOf(c) === cls) same++;
    }
    if (same < n * 0.7) continue;
    const txt = (first.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 50);
    if (!txt) continue;
    out.push({
      tag: el.tagName.toLowerCase(),
      containerCls: (typeof el.className === 'string' ? el.className : '').trim().slice(0, 70),
      childCls: cls.slice(0, 60),
      count: n,
      sample: txt,
      depth: (function(e){let d=0;while(e.parentElement&&d<30){d++;e=e.parentElement}return d})(el)
    });
  }
  out.sort((a, b) => b.count - a.count);
  // 同一 childCls 只保留 count 最大的一个
  const seen = new Set(); const uniq = [];
  for (const o of out) {
    if (seen.has(o.childCls)) continue;
    seen.add(o.childCls);
    uniq.push(o);
  }
  return uniq.slice(0, 25);
}
"""

RISK_TEXT = ["验证码", "安全验证", "操作过于频繁", "请稍后再试", "账号异常", "访问受限"]


def _setup_console() -> None:
    """Windows 控制台默认 GBK，Python 输出 UTF-8 会乱码。

    这里统一转成 GBK，与 bat 里的中文保持一致；
    重定向到文件或管道时（非 tty）保持原样，避免影响日志采集。
    """
    import sys as _s

    if _s.platform != "win32":
        return
    for stream in (_s.stdout, _s.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(encoding="gbk", errors="replace")
        except Exception:
            pass


def c_banner(text: str) -> None:
    line = "─" * 66
    print(f"\n{line}\n{text}\n{line}")


def preflight() -> tuple[bool, str]:
    """检查驱动与浏览器是否就绪。

    报错时必须打出**当前解释器路径**——九成的"装好了却用不了"都是因为
    包装在 A 解释器里，脚本跑在 B 解释器上。
    """
    import sys as _sys

    try:
        import patchright  # noqa: F401
        from patchright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
        hint = [
            "当前解释器里没有 patchright。",
            "",
            f"你现在用的 Python：{_sys.executable}",
            "",
            "本项目依赖装在项目自带的虚拟环境里，不要用系统 python 直接跑。",
            "",
            "正确做法（任选一种）：",
            "  1) 双击 run_probe.bat",
        ]
        if venv_py.exists():
            hint.append(f"  2) 命令行：.venv\\Scripts\\python.exe -m scripts.probe_real")
        else:
            hint.append("  2) 双击 setup.bat 先安装环境（会自动创建 .venv）")
        hint.append("")
        hint.append("如果 .venv 还不存在，先跑 setup.bat。")
        return False, "\n".join(hint)

    chrome_paths = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
    ]
    if not any(p.exists() for p in chrome_paths):
        return False, "未找到系统 Chrome。请先安装 Chrome 稳定版（不要用 Edge 代替，指纹不同）。"

    return True, ""


def check_login(page: Any) -> bool | None:
    """判断登录状态。True 已登录 / False 未登录 / None 无法判断。

    检测顺序按可靠性排：URL > 明确的登录按钮 > 页面文案 > 业务路径。

    ⚠️ 早期版本用 `[class*='avatar']` 这种宽泛选择器判断"已登录"，
    结果未登录页的骨架/推荐位里也可能有 avatar，脚本误判成已登录，
    跳过等待直接探测，几秒后关浏览器——表现就是"还没登录程序就退出了"。
    """
    try:
        url = page.url.lower()
    except Exception:
        return None

    if "login" in url:
        return False

    # 明确的登录入口可见 → 一定未登录
    for sel in (".btn-login", "[class*='login-btn']", "a[ka*='login']", "[class*='sign-in']"):
        try:
            el = page.query_selector(sel)
            if el is not None and el.is_visible():
                return False
        except Exception:
            continue

    # 文案判断（比结构稳，改版后结构没了文案还在）
    body = ""
    try:
        body = page.inner_text("body")[:3000]
    except Exception:
        return None
    for kw in ("扫码登录", "手机号登录", "密码登录", "登录/注册", "验证码登录"):
        if kw in body:
            return False

    # 进了业务路径且上面都没拦住 → 认为已登录
    for path in ("/web/geek/", "/web/boss/", "/web/chat"):
        if path in url:
            return True

    return None


def wait_login(page: Any, timeout_sec: int = 300, force: bool = False) -> bool:
    """等用户手动扫码登录。

    每 15 秒打一次诊断（当前 URL + 判断结果），让用户知道脚本在等什么。
    不做任何自动重试或注入。
    """
    c_banner("等待登录")
    if force:
        print("已指定 --force，跳过登录等待，直接探测当前页面。\n")
        return True

    print("浏览器窗口已经打开。如果显示的是登录页，请扫码登录。")
    print("登录成功后脚本会自动继续，不需要按键。")
    print(f"最长等待 {timeout_sec} 秒。若窗口没出现，说明浏览器启动失败，请看下方报错。\n")

    deadline = time.time() + timeout_sec
    tick = 0
    while time.time() < deadline:
        state = check_login(page)
        if state is True:
            print("\n✓ 检测到已登录，3 秒后开始探测")
            time.sleep(3)
            return True

        if tick % 5 == 0:  # 每 15 秒一次
            try:
                cur = page.url
            except Exception:
                print("\n✗ 浏览器窗口已关闭，探测终止。")
                return False
            remain = int(deadline - time.time())
            label = "未登录" if state is False else "状态未知"
            print(f"  等待中 [{label}] {cur[:70]}  剩余 {remain}s")

        tick += 1
        time.sleep(3)

    print("\n✗ 登录超时。")
    print("  如果窗口里其实已经登录了，说明检测规则没识别出来，")
    print("  可以加 --force 跳过检测直接探测：")
    print("    run_probe.bat --force")
    return False


def probe_selectors(page: Any) -> list[dict[str, Any]]:
    """逐个测试候选选择器，输出命中矩阵。"""
    rows: list[dict[str, Any]] = []
    for target, candidates in GEEK_SELECTORS.items():
        for idx, sel in enumerate(candidates, 1):
            hits = 0
            sample = ""
            try:
                els = page.query_selector_all(sel)
                hits = len(els)
                if hits:
                    try:
                        sample = (els[0].inner_text() or "").replace("\n", " ").strip()[:36]
                    except Exception:
                        sample = ""
            except Exception:
                hits = -1
            rows.append({
                "target": target,
                "candidate": idx,
                "selector": sel,
                "hits": hits,
                "sample": sample,
            })
    return rows


def probe_risk(page: Any) -> list[str]:
    hits: list[str] = []
    try:
        body = page.inner_text("body")[:5000]
    except Exception:
        body = ""
    for kw in RISK_TEXT:
        if kw in body:
            hits.append(f"页面文案出现「{kw}」")
    for sel in ["#nc_1_wrapper", "iframe[src*='captcha']", "[class*='captcha']", "[class*='verify']"]:
        try:
            if page.query_selector(sel) is not None:
                hits.append(f"检测到验证控件：{sel}")
        except Exception:
            continue
    return hits


def classify(rows: list[dict[str, Any]], logged_in: bool = False) -> list[dict[str, Any]]:
    """把命中矩阵压成"每个目标用第几个候选"。

    已登录时页面上不会有登录按钮，这一项必然全未命中——
    不是选择器错了，所以标成 N/A，避免误导用户去手工补。
    """
    out = []
    targets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        targets.setdefault(r["target"], []).append(r)
    for target, items in targets.items():
        if logged_in and target == "登录按钮":
            out.append({
                "target": target, "ok": True, "best_selector": "",
                "best_hits": 0, "verdict": "N/A（已登录，属预期）", "candidates": items,
            })
            continue
        best = max(items, key=lambda x: x["hits"])
        if best["hits"] > 0:
            verdict = f"用第 {best['candidate']} 个"
            ok = True
        else:
            verdict = "全部未命中，需手动找"
            ok = False
        out.append({
            "target": target,
            "ok": ok,
            "best_selector": best["selector"] if ok else "",
            "best_hits": best["hits"],
            "verdict": verdict,
            "candidates": items,
        })
    return out


def render_report(
    page_url: str, page_title: str, login: bool, risk: list[str],
    summary: list[dict[str, Any]], sniff: list[dict[str, Any]], shot: str, html: str,
) -> str:
    lines: list[str] = []
    lines.append("# 真实环境探测报告\n")
    lines.append(f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 页面：{page_title}")
    lines.append(f"- 地址：{page_url}")
    lines.append(f"- 登录态：{'已登录' if login else '未登录'}")
    lines.append(f"- 风控信号：{'、'.join(risk) if risk else '未检测到'}")
    lines.append(f"- 截图：{shot}")
    lines.append(f"- HTML 快照：{html}\n")

    lines.append("## 一、选择器体检\n")
    lines.append("| 目标 | 结论 | 最佳命中数 | 采用的选择器 |")
    lines.append("|---|---|---|---|")
    for s in summary:
        lines.append(f"| {s['target']} | {s['verdict']} | {s['best_hits']} | `{s['best_selector']}` |")

    lines.append("\n## 二、逐候选明细\n")
    for s in summary:
        lines.append(f"### {s['target']}\n")
        lines.append("| # | 选择器 | 命中 | 样本 |")
        lines.append("|---|---|---|---|")
        for c in s["candidates"]:
            sample = c["sample"].replace("|", "/") if c["sample"] else "—"
            lines.append(f"| {c['candidate']} | `{c['selector']}` | {c['hits']} | {sample[:36]} |")
        lines.append("")

    if sniff:
        lines.append("## 三、页面结构嗅探（自动发现的列表容器）\n")
        lines.append("下面这些是页面上「子元素结构一致」的容器，按数量排序。")
        lines.append("如果上面的体检有多项没命中，从这里找真实 class。\n")
        lines.append("| 容器 class | 子元素 class | 数量 | 首条样本 |")
        lines.append("|---|---|---|---|")
        for s in sniff:
            sample = str(s.get("sample", "")).replace("|", "/")[:40]
            lines.append(f"| `{s.get('containerCls') or s.get('tag')}` | `{s.get('childCls')}` | {s.get('count')} | {sample} |")
        lines.append("")

    lines.append("## 四、下一步\n")
    lines.append("1. 把「用第 N 个」的选择器，填进 `config/selectors.yaml` 对应字段的最前面（招聘端要多校准一遍，两边结构不同）。")
    lines.append("2. 全部未命中的项，用浏览器 F12 → 右键元素 → Copy selector 手动补。")
    lines.append("3. 如果风控信号有内容，先降低频率再继续，不要立刻重试。")
    return "\n".join(lines)


def main() -> int:
    _setup_console()
    ap = argparse.ArgumentParser(description="真实环境只读探针")
    ap.add_argument("--url", default="https://www.zhipin.com/web/geek/job-recommend",
                    help="要探测的页面地址（默认求职端推荐职位页）")
    ap.add_argument("--timeout", type=int, default=240, help="等待登录的秒数")
    ap.add_argument("--keep-open", action="store_true", help="探测完保留浏览器窗口")
    ap.add_argument("--dry-run", action="store_true",
                    help="只验证驱动层（启动浏览器打开空白页），不访问 BOSS")
    ap.add_argument("--force", action="store_true",
                    help="跳过登录等待，直接探测当前页面（已登录但检测不出来时用）")
    ap.add_argument("--no-pause", action="store_true",
                    help="（已废弃，保留兼容）探测结束后不等回车")
    ap.add_argument("--profile", default="",
                    help="自定义登录态目录（默认 data/chrome-profile-geek）；被占用时可换一个")
    args = ap.parse_args()

    c_banner("AI 招聘助手 · 真实环境探针")
    print("本工具只做读取和诊断，不发消息、不投递、不改账号状态。\n")

    ok, msg = preflight()
    if not ok:
        print("✗ 前置检查未通过：")
        print("  " + msg.replace("\n", "\n  "))
        return 1
    print("✓ patchright 与 Chrome 就绪")

    from patchright.sync_api import sync_playwright

    global PROFILE_DIR
    if args.profile:
        PROFILE_DIR = Path(args.profile).expanduser()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_DIR / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    pw = sync_playwright().start()
    ctx = None
    try:
        print(f"✓ 登录态目录：{PROFILE_DIR}")
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",          # 用系统真实 Chrome，TLS 指纹才真实
                headless=False,            # 必须可见：无头有一整套独立特征
                viewport=None,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )
        except Exception as exc:
            msg = str(exc)
            print("\n✗ 浏览器启动失败\n")
            occupied_hints = (
                "ProcessSingleton", "already in use", "user data directory is already in use",
                "此会话", "另一个程序", "另一个会话",   # Chrome 简体中文的实际提示
            )
            if any(h in msg for h in occupied_hints):
                print("原因：这个 profile 目录被另一个 Chrome 进程占用了。")
                print("处理：关掉所有由本工具弹出的 Chrome 窗口，或结束残留的 chrome.exe 进程，再重试。")
                print(f"      或换一个登录态目录：run_probe.bat --profile data/profile2")
                print(f"      也可以直接删掉目录后重跑（会丢登录态）：{PROFILE_DIR}")
            elif "Executable doesn't exist" in msg or "channel" in msg:
                print("原因：找不到系统 Chrome。")
                print("处理：安装 Chrome 稳定版（不要用 Edge 代替，指纹不同）。")
            else:
                print("原因：")
                print("  " + msg.replace("\n", "\n  "))
                print("\n如果反复失败，先试 --dry-run 确认驱动层，再把上面的信息发出来。")
            return 1
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if args.dry_run:
            # 只验证驱动层：起浏览器 + 打开空白页 + 读基本指纹，不碰 BOSS
            page.goto("about:blank")
            info = page.evaluate("""() => ({
                webdriver: navigator.webdriver,
                ua: navigator.userAgent,
                plugins: navigator.plugins.length,
                langs: navigator.languages,
                hasChrome: typeof window.chrome !== 'undefined',
                hardware: navigator.hardwareConcurrency,
            })""")
            c_banner("驱动层自检结果")
            print(f"  navigator.webdriver  = {info.get('webdriver')}   （None/false 才正常）")
            print(f"  window.chrome 存在   = {info.get('hasChrome')}")
            print(f"  plugins 数量         = {info.get('plugins')}   （0 通常是 headless 特征）")
            print(f"  语言                 = {info.get('langs')}")
            print(f"  硬件并发             = {info.get('hardware')}")
            print(f"  UA                   = {str(info.get('ua'))[:90]}")
            print()
            if info.get("webdriver"):
                print("  ✗ navigator.webdriver 为 true，说明驱动没生效，检查是否用的是 patchright")
            elif not info.get("hasChrome"):
                print("  ⚠ window.chrome 缺失，可能不是真实 Chrome")
            else:
                print("  ✓ 驱动层正常，可以继续登录探测（去掉 --dry-run）")
            return 0

        print(f"→ 打开 {args.url}")
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"  页面加载异常（不影响后续）：{exc}")
        time.sleep(3)

        if not wait_login(page, args.timeout, force=args.force):
            print("\n探测终止。登录后重新运行即可（登录态会保留在 profile 目录）。")
            return 2

        try:
            cur_url = page.url
            cur_state = check_login(page)
        except Exception:
            cur_url, cur_state = "（读取失败）", None
        print(f"\n→ 开始探测（只读）")
        print(f"  当前页面：{cur_url}")
        print(f"  登录判断：{'已登录' if cur_state else ('未登录' if cur_state is False else '状态未知')}")
        time.sleep(2)

        # 风控
        risk = probe_risk(page)
        if risk:
            print("⚠ 检测到风控信号：")
            for r in risk:
                print("   - " + r)

        # 选择器体检
        rows = probe_selectors(page)
        summary = classify(rows, logged_in=(check_login(page) is True))

        # 结构嗅探
        sniff: list[dict[str, Any]] = []
        try:
            sniff = page.evaluate(SNIFF_JS) or []
        except Exception as exc:
            print(f"  结构嗅探失败：{exc}")

        # 快照
        shot_path = out_dir / "snapshot.png"
        try:
            page.screenshot(path=str(shot_path), full_page=False)
        except Exception:
            shot_path = Path("")

        html_path = out_dir / "page.html"
        try:
            html_path.write_text(page.content(), encoding="utf-8", errors="ignore")
        except Exception:
            pass

        title = ""
        try:
            title = page.title()
        except Exception:
            pass

        report = render_report(
            page.url, title, cur_state is True, risk, summary, sniff,
            str(shot_path), str(html_path),
        )
        (out_dir / "report.md").write_text(report, encoding="utf-8")
        (out_dir / "report.json").write_text(
            json.dumps({
                "url": page.url, "title": title, "risk": risk,
                "selectors": summary, "sniff": sniff,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 控制台摘要
        c_banner("选择器体检结果")
        for s in summary:
            mark = "✓" if s["ok"] else "✗"
            sel = f"  {s['best_selector']}" if s["ok"] else ""
            print(f"  {mark} {s['target']:<12} 命中 {s['best_hits']:<3} {s['verdict']}{sel}")

        if sniff:
            c_banner("页面结构嗅探（前 6 条）")
            for s in sniff[:6]:
                print(f"  [{s.get('count'):>3}] {s.get('containerCls') or s.get('tag')} > {s.get('childCls')}")
                print(f"        {str(s.get('sample',''))[:56]}")

        c_banner("✓ 探测完成，浏览器已关闭")
        print(f"  报告：{out_dir / 'report.md'}")
        print(f"  截图：{shot_path if shot_path else '（失败）'}")
        print(f"  快照：{html_path}")
        print()
        print("  把 report.md 发出来，即可据此校准选择器。")
        print("  想保留浏览器现场看页面：带 --keep-open 再跑一次。")

        if args.keep_open:
            c_banner("浏览器保持打开")
            print("按 Ctrl+C 结束。登录态已保存在 profile 目录，下次免登录。")
            while True:
                time.sleep(5)

        return 0

    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as exc:
        print(f"\n✗ 探测异常：{exc}")
        import traceback

        traceback.print_exc()
        return 1
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


if __name__ == "__main__":
    raise SystemExit(main())
