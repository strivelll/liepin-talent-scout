"""真实发送招呼。**只在确认过预览之后执行。**

这是整个求职端唯一不可撤销的动作：消息发出去就进了 HR 的会话列表，
占用账号当日额度，对方会收到通知。

所以设计成必须显式确认：

    python -m scripts.send_greetings --limit 10 --confirm

没有 --confirm 就只做检查、不发送。

**发送前的三道检查**（脚本会自动做）：
1. 登录态是否有效
2. 风控信号是否正常
3. 当日额度是否够

**发送节奏**：每条之间随机等 25-60 秒（可配）。不是保守，是必须——
连续快速打招呼是触发风控最快的方式。

**中途熔断**：任何一条检测到验证码/限流，立刻停，不再继续。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.browser.seeker_driver import RealSeekerDriver  # noqa: E402
from app.config import config  # noqa: E402
from app.seeker import scripts as script_gen  # noqa: E402
from app.seeker.matcher import JobInfo, run_match  # noqa: E402
from app.seeker.profile import load_profile  # noqa: E402

OUT_DIR = ROOT / "data" / "send-logs"


def _console() -> None:
    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            try:
                if s.isatty():
                    s.reconfigure(encoding="gbk", errors="replace")
            except Exception:
                pass


def load_rows(spec: str = "") -> tuple[list[dict], list[str]]:
    """加载岗位池，支持合并多次抓取。

    单次关键词覆盖有限：'Java' 抓不到标题写'后端'的岗，'后端' 又会混进催收/班主任。
    所以默认合并最近两次抓取，去重后一起筛。
    """
    base = ROOT / "data" / "probe-filter"
    paths: list[Path] = []
    if spec:
        paths = [Path(x.strip()) for x in spec.split(",") if x.strip()]
    elif base.exists():
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
                continue
            seen.add(key)
            rows.append(r)
    return rows, used


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
    ap = argparse.ArgumentParser(description="真实发送打招呼")
    ap.add_argument("--limit", type=int, default=10, help="最多发几条")
    ap.add_argument("--confirm", action="store_true", help="必须加此参数才会真的发送")
    ap.add_argument("--report", default="", help="指定抓取结果")
    ap.add_argument("--interval", default="", help="覆盖间隔秒数，如 25-60")
    ap.add_argument("--skip", type=int, default=0, help="跳过前 N 条（续发时用，避免重复投递）")
    args = ap.parse_args()

    print("=" * 74)
    print("真实发送打招呼")
    print("=" * 74)

    if not args.confirm:
        print("✗ 缺少 --confirm。")
        print("  先跑预览确认内容：python -m scripts.preview_greetings --limit 10")
        print("  确认无误后再加 --confirm 执行。")
        return 1

    rows, used = load_rows(args.report)
    if not rows:
        print("✗ 找不到抓取结果。先跑：python -m scripts.probe_filter --query Java --city 长沙")
        return 1
    print(f"岗位池：{' + '.join(used)}，合并去重 {len(rows)} 个")
    profile = load_profile()

    # 选出要发的
    todo: list[tuple[JobInfo, object, str]] = []
    for i, r in enumerate(rows):
        job = row_to_job(r, i)
        m = run_match(job, profile)
        if not m.passed:
            continue
        todo.append((job, m, script_gen.greeting(job, m, profile, seed=i)))
        if len(todo) >= args.limit + args.skip:
            break

    if args.skip:
        print(f"跳过前 {args.skip} 条（已投递过）")
        todo = todo[args.skip:]
    if not todo:
        print("✗ 没有可发的岗位")
        return 1

    # 额度检查
    cap = int(config.seeker.get("run", {}).get("daily_greet_limit", 30) or 30)
    used = db.count_seek_greeted_today()
    left = max(0, cap - used)
    print(f"当日额度：{used}/{cap}，剩余 {left}")
    todo = todo[:left]
    if not todo:
        print("✗ 今日额度已用完")
        return 1

    # 间隔
    if args.interval:
        lo, hi = [float(x) for x in args.interval.split("-")]
    else:
        iv = config.seeker.get("run", {}).get("greet_interval", [25, 60])
        lo, hi = float(iv[0]), float(iv[1])
    print(f"发送节奏：每条间隔 {lo:.0f}-{hi:.0f} 秒")
    print(f"计划发送：{len(todo)} 条")
    print()
    for n, (job, m, text) in enumerate(todo, 1):
        print(f"{n:>2}. {job.title[:22]}｜{job.company[:14]}｜{job.salary_text}")
    print()
    print("3 秒后开始，Ctrl+C 可中止…")
    time.sleep(3)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = OUT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    log: list[dict] = []

    drv = RealSeekerDriver()
    ok = 0
    try:
        drv.open()
        drv.page.goto(
            f"https://www.zhipin.com/web/geek/jobs?query="
            f"{config.seeker.get('run', {}).get('query', 'Java')}"
            f"&city={config.seeker.get('run', {}).get('city_code', '101250100')}",
            wait_until="domcontentloaded", timeout=45000,
        )
        time.sleep(8)   # 列表页渲染需要时间，等太短会找不到卡片

        for n, (job, m, text) in enumerate(todo, 1):
            # 每发一条前先查风控
            sig = drv.risk_signal()
            if sig:
                print(f"\n⚠ 检测到 {sig}，立即停止（已发 {ok} 条）")
                log.append({"stopped": sig, "sent_so_far": ok})
                break

            print(f"\n[{n}/{len(todo)}] {job.title[:20]} … ", end="", flush=True)
            try:
                sent = drv.greet(job, text)
            except Exception as exc:
                print(f"异常 {exc}")
                log.append({"title": job.title, "ok": False, "error": str(exc)})
                continue

            if sent:
                ok += 1
                print("✓ 已发送")
                db.bump_seek_stat("greeted")
                jid = db.upsert_seek_job({
                    **job.to_row(),
                    "jd_text": "",
                })
                db.update_seek_job(jid, state="greeted", greeting=text,
                                   match_score=m.score, match_reason=m.reason,
                                   match_pitch=m.pitch)
                db.add_seek_message(jid, "me", text)
                log.append({"title": job.title, "company": job.company,
                            "salary": job.salary_text, "text": text, "ok": True})
            else:
                print("✗ 失败")
                log.append({"title": job.title, "ok": False})

            if n < len(todo):
                wait = random_uniform(lo, hi)
                print(f"      等待 {wait:.0f}s …", end="", flush=True)
                time.sleep(wait)
                print("\r", end="")

    except KeyboardInterrupt:
        print("\n已手动中止")
    except Exception as exc:
        print(f"\n✗ 执行异常：{exc}")
        import traceback

        traceback.print_exc()
    finally:
        try:
            drv.close()
        except Exception:
            pass
        log_path.write_text(json.dumps({"at": datetime.now().isoformat(),
                                        "sent": ok, "detail": log},
                                       ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 74)
    print(f"完成：成功发送 {ok} 条 / 计划 {len(todo)} 条")
    print(f"记录：{log_path}")
    print("=" * 74)
    return 0


def random_uniform(lo: float, hi: float) -> float:
    import random

    return random.uniform(lo, hi)


if __name__ == "__main__":
    raise SystemExit(main())
