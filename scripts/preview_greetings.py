"""打招呼预览：只算不发，把要发的每条内容摊开给人看。

**为什么要这个脚本**

发送是不可撤销的。10 条招呼就是 10 次真实投递：占用账号当日额度、
进入 10 个 HR 的会话列表、对方会收到通知。发错了没法撤回，只能道歉。

所以在真实发送前，必须先把"发给谁、发什么"完整列出来核对。
这个脚本就是那道闸门。

用法：
    python -m scripts.preview_greetings                 # 用最新一次抓取结果
    python -m scripts.preview_greetings --limit 10      # 指定要看几条
    python -m scripts.preview_greetings --report data/probe-filter/xxx/report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.seeker import scripts as script_gen  # noqa: E402
from app.seeker.matcher import JobInfo, run_match  # noqa: E402
from app.seeker.profile import load_profile  # noqa: E402


def _console() -> None:
    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            try:
                if s.isatty():
                    s.reconfigure(encoding="gbk", errors="replace")
            except Exception:
                pass


def latest_report() -> Path | None:
    base = ROOT / "data" / "probe-filter"
    if not base.exists():
        return None
    dirs = sorted([d for d in base.iterdir() if d.is_dir() and (d / "report.json").exists()])
    return (dirs[-1] / "report.json") if dirs else None


def row_to_job(r: dict, idx: int) -> JobInfo:
    return JobInfo(
        ext_id=f"r{idx:03d}",
        title=str(r.get("title", "")),
        company=str(r.get("company", "")),
        salary_text=str(r.get("salary", "")),
        salary_min=r.get("salary_min"),
        salary_max=r.get("salary_max"),
        city=str(r.get("city", "")),
        district=str(r.get("district", "") or r.get("street", "")),
        tags=[str(t) for t in (r.get("tags") or [])],
    )


def main() -> int:
    _console()
    ap = argparse.ArgumentParser(description="打招呼预览（只算不发）")
    ap.add_argument("--report", default="",
                    help="指定 report.json，多个用逗号分隔；默认用最新的两个")
    ap.add_argument("--limit", type=int, default=10, help="要发送的条数")
    args = ap.parse_args()

    # 支持合并多次抓取：单次关键词覆盖有限，"Java" 和 "后端" 各有遗漏
    paths: list[Path] = []
    if args.report:
        paths = [Path(x.strip()) for x in args.report.split(",") if x.strip()]
    else:
        base = ROOT / "data" / "probe-filter"
        dirs = sorted([d for d in base.iterdir()
                       if d.is_dir() and (d / "report.json").exists()])[-2:]
        paths = [d / "report.json" for d in dirs]

    rows: list[dict] = []
    seen: set[str] = set()
    used: list[str] = []
    for pth in paths:
        if not pth.exists():
            continue
        data = json.loads(pth.read_text(encoding="utf-8"))
        used.append(pth.parent.name)
        for r in (data.get("rows") or data.get("jobs") or []):
            key = f"{r.get('title','')}|{r.get('company','')}|{r.get('salary','')}"
            if key in seen:
                continue          # 两次抓取会重叠，去重
            seen.add(key)
            rows.append(r)

    if not rows:
        print("✗ 找不到抓取结果。先跑：python -m scripts.probe_filter --query Java --city 长沙")
        return 1

    print("=" * 74)
    print("打招呼预览（不会发送任何消息）")
    print("=" * 74)
    print(f"数据来源：{' + '.join(used)}　合并去重后 {len(rows)} 个岗位")
    profile = load_profile()
    print(f"求职者：{profile.name}　{profile.education}　{profile.city}　"
          f"期望 {profile.salary_min // 1000}-{profile.salary_max // 1000}K")
    print(f"技能：{'、'.join(profile.skills)}")
    print()

    passed: list[tuple[JobInfo, object, str]] = []
    skipped: list[tuple[JobInfo, object]] = []

    for i, r in enumerate(rows):
        job = row_to_job(r, i)
        m = run_match(job, profile)
        if not m.passed:
            skipped.append((job, m))
            continue
        text = script_gen.greeting(job, m, profile, seed=i)
        passed.append((job, m, text))

    print("-" * 74)
    print(f"通过筛选：{len(passed)} 条　未通过：{len(skipped)} 条")
    print("-" * 74)
    print()
    print(f"将发送前 {min(args.limit, len(passed))} 条：")
    print()

    for n, (job, m, text) in enumerate(passed[: args.limit], 1):
        print(f"{n:>2}. 【{job.title[:26]}】{job.company[:16]} {job.salary_text}")
        print(f"    匹配 {m.score}｜{m.reason[:60]}")
        print(f"    ▶ 发送：{text}")
        print(f"    （{len(text)} 字）")
        print()

    if len(passed) < args.limit:
        print(f"⚠ 只匹配到 {len(passed)} 条，不够 {args.limit} 条。")
        print("   可以：放宽 salary 上下限、加城市、或换更大范围的关键词。")
        print()

    if skipped:
        print("-" * 74)
        print("未通过的原因（供核对筛得对不对）")
        print("-" * 74)
        for job, m in skipped:
            print(f"  ✗ {job.title[:24]:<26}{m.reason[:44]}")
        print()

    print("=" * 74)
    print("以上均未发送。确认无误后执行：")
    print("    python -m scripts.send_greetings --limit 10 --confirm")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
