"""职位筛选验证：搜索、筛条件、读结果。

**为什么单独做一个脚本**

求职端的"筛岗位"和招聘端的"筛候选人"是镜像关系：
- 求职端：按关键词/城市/薪资/经验 筛职位
- 招聘端：按年龄/学历/年限/活跃度 筛候选人

两边的**操作方式一样**（都是填条件、读列表），所以这里验证通的交互模式，
招聘端可以直接复用。区别只在选择器和字段。

**三种筛法，风险与稳定性递增**

1. URL 参数：`/web/geek/jobs?query=运营&city=101250100`
   最稳、最快、零点击。缺点是只能传部分条件。
2. 筛选器 UI：点"薪资范围""经验要求"等下拉。
   能覆盖全部条件，但要处理弹层、选中态、异步刷新。
3. 搜索框输入：模拟打字。
   最像真人，但最慢，且容易因为输入法/联想词出问题。

本脚本三种都实现，默认用 URL（稳），`--via ui` 切换。

用法：
    python -m scripts.probe_filter --query 运营
    python -m scripts.probe_filter --query 运营 --city 长沙
    python -m scripts.probe_filter --query 运营 --via ui --salary 5-10K
    python -m scripts.probe_filter --query 运营 --explore    # 只探查筛选器结构
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app import boss_font

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROFILE_DIR = ROOT / "data" / "chrome-profile-geek"
OUT_DIR = ROOT / "data" / "probe-filter"
BASE = "https://www.zhipin.com"

# 常用城市码（BOSS 的城市编码）。不传就用当前 IP 定位的城市。
CITY_CODES: dict[str, str] = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "武汉": "101200100",
    "南京": "101190100",
    "苏州": "101190400",
    "长沙": "101250100",
    "西安": "101110100",
    "重庆": "101040100",
    "天津": "101030100",
    "郑州": "101180100",
    "东莞": "101281600",
    "佛山": "101280800",
    "合肥": "101220100",
    "福州": "101230100",
    "厦门": "101230200",
}

# 结果卡片内的字段选择器（2026-10-21 实测）
CARD_SEL: dict[str, list[str]] = {
    "卡片": [".job-card-wrap", ".job-card-box"],
    "标题": [".job-name", ".job-title", "a[href*='/job_detail/']"],
    # 薪资是字体混淆的，取到的是 PUA 字符，必须过 boss_font.decode
    "薪资": [".job-salary", ".salary"],
    "标签": [".tag-list li"],
    # 实测：.boss-name 里装的是**公司名**（如"为华企业管理有限公司"），不是 HR 姓名
    "公司": [".boss-name", "[class*='company-name']"],
    # .company-location 形如 " 长沙·开福区·湘江中路 "
    "地区": [".company-location", "[class*='location']"],
}

# 筛选器区域（用于 --explore 输出结构）
FILTER_SEL: dict[str, list[str]] = {
    "筛选区容器": [".filter-box", ".search-filter", "[class*='filter']", "[class*='condition']"],
    "薪资筛选项": ["[class*='salary']", "[class*='filter'] li"],
    "经验筛选项": ["[class*='experience']", "[class*='exp'] li"],
    "学历筛选项": ["[class*='degree']", "[class*='edu'] li"],
    "城市选择": ["[class*='city']", "[class*='current-city']"],
    "搜索框": ["input[class*='search']", ".search-input input", "input[name='query']", "[class*='search'] input"],
}

RISK_TEXT = ["验证码", "安全验证", "操作过于频繁", "请稍后再试", "账号异常", "访问受限"]


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


def txt(el: Any) -> str:
    try:
        return (el.inner_text() or "").replace("\n", " ").strip()
    except Exception:
        return ""


def pick(scope: Any, name: str) -> tuple[Any, str]:
    for sel in CARD_SEL.get(name, []):
        try:
            el = scope.query_selector(sel)
            if el is not None:
                return el, sel
        except Exception:
            continue
    return None, ""


def pick_all(scope: Any, name: str) -> list[str]:
    for sel in CARD_SEL.get(name, []):
        try:
            els = scope.query_selector_all(sel)
            if els:
                return [txt(e) for e in els if txt(e)]
        except Exception:
            continue
    return []


def risk(page: Any) -> list[str]:
    hits: list[str] = []
    try:
        body = page.inner_text("body")[:6000]
    except Exception:
        return hits
    for kw in RISK_TEXT:
        if kw in body:
            hits.append(kw)
    for sel in ["#nc_1_wrapper", "iframe[src*='captcha']", "[class*='captcha']"]:
        try:
            if page.query_selector(sel) is not None:
                hits.append(f"控件:{sel}")
        except Exception:
            continue
    return hits


def split_company_location(raw: str) -> tuple[str, str]:
    """把 ".company-location" 的文本拆成（公司, 地区）。

    实测文本长这样："长沙 岳麓区" 或 "某某科技有限公司 · 长沙"，
    中间可能是空格、中点、竖线。拆不出来就整体归到公司。
    """
    raw = raw.strip()
    for sep in ("·", "|", "｜", "，"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            return left.strip(), right.strip()
    # 无分隔符时，若结尾是"市/区/县"，尝试从最后一个空格切
    if " " in raw:
        left, right = raw.rsplit(" ", 1)
        if any(k in right for k in ("市", "区", "县", "镇", "路")):
            return left.strip(), right.strip()
    return raw, ""


def split_area(raw: str) -> tuple[str, str, str]:
    """把 "长沙·开福区·湘江中路" 拆成 (城市, 区, 街道)。"""
    parts = [p.strip() for p in raw.replace(" ", "").split("·") if p.strip()]
    city = parts[0] if len(parts) > 0 else ""
    district = parts[1] if len(parts) > 1 else ""
    street = parts[2] if len(parts) > 2 else ""
    return city, district, street


def extract(page: Any, limit: int = 20) -> list[dict[str, Any]]:
    cards: list[Any] = []
    for sel in CARD_SEL["卡片"]:
        try:
            cards = list(page.query_selector_all(sel))
            if cards:
                break
        except Exception:
            continue

    rows: list[dict[str, Any]] = []
    for c in cards[:limit]:
        title_el, _ = pick(c, "标题")
        salary_el, _ = pick(c, "薪资")
        comp_el, _ = pick(c, "公司")
        loc_el, _ = pick(c, "地区")

        # 薪资必须解码：DOM 里是 PUA 字符，直接读会得到 "-K" 这种残缺值
        salary_raw = txt(salary_el) if salary_el is not None else ""
        salary = boss_font.parse_salary(salary_raw)

        city, district, street = split_area(txt(loc_el) if loc_el is not None else "")

        rows.append({
            "title": txt(title_el) if title_el is not None else "",
            "salary": salary["text"],
            "salary_min": salary["min"],
            "salary_max": salary["max"],
            "salary_months": salary["months"],
            "salary_unknown": salary["unknown_chars"],
            "company": txt(comp_el) if comp_el is not None else "",
            "city": city,
            "district": district,
            "street": street,
            "tags": pick_all(c, "标签")[:6],
        })
    return rows


def show(rows: list[dict[str, Any]]) -> None:
    banner(f"筛选结果：{len(rows)} 条")
    print(f"  {'职位':<22}{'薪资':<14}{'公司':<20}{'地区':<12}")
    print(f"  {'-' * 20:<22}{'-' * 12:<14}{'-' * 18:<20}{'-' * 10:<12}")
    bad = 0
    for r in rows:
        sal = r["salary"] or "面议"
        # 解码后的文本通常已经含 "·13薪"，只在没含时才补，否则会显示两次
        if r.get("salary_months") and "薪" not in sal:
            sal += f"·{r['salary_months']}薪"
        area = f"{r['district']}{r['street']}" or r["city"]
        print(f"  {r['title'][:20]:<22}{sal[:12]:<14}{r['company'][:18]:<20}{area[:10]:<12}")
        if r["tags"]:
            print(f"      {' / '.join(r['tags'][:4])}")
        if r.get("salary_unknown"):
            bad += 1
    if bad:
        print(f"\n  ⚠ {bad} 条薪资有未识别字符，字体映射可能已过期（见 app/boss_font.py）")


def explore_filters(page: Any) -> dict[str, Any]:
    """探查筛选器结构（只读）。"""
    banner("筛选器结构探查")
    out: dict[str, Any] = {}
    for name, sels in FILTER_SEL.items():
        for sel in sels:
            try:
                els = page.query_selector_all(sel)
            except Exception:
                continue
            if els:
                samples = [txt(e)[:18] for e in els[:8] if txt(e)]
                print(f"  ✓ {name:<10} {len(els):>3}  {sel}")
                if samples:
                    print(f"      {' | '.join(samples[:6])}")
                out[name] = {"selector": sel, "count": len(els), "samples": samples}
                break
        else:
            print(f"  ✗ {name:<10}   0  （已试 {len(sels)} 个候选）")
    return out


def build_url(query: str, city: str = "") -> str:
    url = f"{BASE}/web/geek/jobs?query={quote(query)}"
    code = CITY_CODES.get(city, "")
    if code:
        url += f"&city={code}"
    return url


def main() -> int:
    _console()
    ap = argparse.ArgumentParser(description="求职端职位筛选验证")
    ap.add_argument("--query", default="运营", help="搜索关键词")
    ap.add_argument("--city", default="", help="城市中文名，如 长沙 / 深圳；留空用 IP 定位")
    ap.add_argument("--via", choices=["url", "ui", "input"], default="url",
                    help="url=直接构造地址(最稳) / ui=点筛选器 / input=在搜索框打字")
    ap.add_argument("--salary", default="", help="UI 模式下的薪资条件，如 5-10K")
    ap.add_argument("--experience", default="", help="UI 模式下的经验条件，如 1-3年")
    ap.add_argument("--limit", type=int, default=20, help="最多读多少条")
    ap.add_argument("--salary-min", type=int, default=0, help="薪资下限（元/月），0 表示不限")
    ap.add_argument("--salary-max", type=int, default=0, help="薪资上限（元/月），0 表示不限")
    ap.add_argument("--explore", action="store_true", help="只探查筛选器结构，不搜索")
    ap.add_argument("--profile", default="")
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()

    global PROFILE_DIR
    if args.profile:
        PROFILE_DIR = Path(args.profile).expanduser()

    banner("求职端职位筛选验证")
    print(f"关键词：{args.query}　城市：{args.city or '（IP 定位）'}　方式：{args.via}")

    try:
        from patchright.sync_api import sync_playwright
    except Exception:
        print("✗ 未安装 patchright，请先运行 setup.bat")
        return 1

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_DIR / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    pw = sync_playwright().start()
    ctx = None
    code = 0
    report: dict[str, Any] = {"at": stamp, "query": args.query, "city": args.city, "via": args.via}

    def logged(p: Any) -> bool:
        try:
            u = p.url.lower()
        except Exception:
            return False
        if "/web/user/" in u or "login" in u:
            return False
        try:
            b = p.inner_text("body")[:3000]
        except Exception:
            return True
        return not any(k in b for k in ("扫码登录", "手机号登录", "密码登录"))

    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome", headless=False, viewport=None,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        url = build_url(args.query, args.city)
        print(f"\n→ 打开 {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"  加载异常（继续）：{exc}")
        time.sleep(3)

        if not logged(page):
            banner("需要登录")
            print("请在浏览器里扫码登录，最多等 180 秒。")
            deadline = time.time() + 180
            while time.time() < deadline and not logged(page):
                time.sleep(3)
            if not logged(page):
                print("✗ 登录超时")
                return 2
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            time.sleep(3)

        r0 = risk(page)
        print(f"  风控信号：{'、'.join(r0) if r0 else '未检测到'}")
        report["risk_before"] = r0

        if args.explore:
            report["filters"] = explore_filters(page)
        else:
            # UI / input 模式需要额外操作
            if args.via == "ui":
                banner("点击筛选条件（UI 模式）")
                report["filters"] = explore_filters(page)
                for label, val in (("薪资", args.salary), ("经验", args.experience)):
                    if not val:
                        continue
                    hit = False
                    for sel in (f"text={val}", f"[class*='filter'] li:has-text('{val}')"):
                        try:
                            el = page.query_selector(sel)
                            if el is not None:
                                el.click()
                                time.sleep(2)
                                print(f"  ✓ 已选{label}：{val}")
                                hit = True
                                break
                        except Exception:
                            continue
                    if not hit:
                        print(f"  ✗ 没找到{label}选项「{val}」，该条件未生效")
                time.sleep(2)

            elif args.via == "input":
                banner("在搜索框输入（input 模式）")
                box = None
                for sel in FILTER_SEL["搜索框"]:
                    try:
                        box = page.query_selector(sel)
                        if box is not None:
                            print(f"  ✓ 搜索框：{sel}")
                            break
                    except Exception:
                        continue
                if box is None:
                    print("  ✗ 没找到搜索框，退回 URL 方式")
                else:
                    try:
                        box.click()
                        for ch in args.query:
                            box.type(ch, delay=0)
                            time.sleep(0.06)
                        box.press("Enter")
                        time.sleep(4)
                        print("  已输入并回车")
                    except Exception as exc:
                        print(f"  输入失败：{exc}")

            rows = extract(page, limit=args.limit)

            # 本地按薪资再筛一道（用交集判断，不是包含）
            total = len(rows)
            if args.salary_min or args.salary_max:
                lo = args.salary_min or None
                hi = args.salary_max or None
                rows = [r for r in rows if boss_font.in_range(
                    {"min": r["salary_min"], "max": r["salary_max"]}, lo, hi)]
                kept = len(rows)
                print(f"\n  薪资过滤：{lo or '不限'} ~ {hi or '不限'}　保留 {kept}/{total} 条")

            show(rows)
            report["rows"] = rows

            r1 = risk(page)
            if r1:
                print(f"\n  ⚠ 搜索后出现风控信号：{'、'.join(r1)}")
            report["risk_after"] = r1
            report["url_final"] = page.url

            try:
                page.screenshot(path=str(out_dir / "result.png"), full_page=False)
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
        print(f"\n✗ 异常：{exc}")
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
