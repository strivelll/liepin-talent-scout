"""端到端冒烟测试：用 Mock 驱动跑通完整链路。

跑法：
    python -m scripts.smoke_test

它验证的是业务逻辑，不是 UI：
  筛选 → 打招呼 → 多轮对话 → 判定 → 邀约 → 日报

输出会打出每个候选人的最终状态，用于确认每条分支都被走到。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.config import config  # noqa: E402
from app.core import pipeline  # noqa: E402


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


def reset_data() -> None:
    """清空上一轮的数据，保证每次跑都是干净状态。

    注意：不能直接删 .db 文件——SQLite 连接还持有句柄，Windows 上会删不掉，
    结果是旧的 daily_stats 残留，统计数字越来越离谱。清表才是可靠做法。
    """
    db.init_db()
    conn = db.get_conn()
    for table in ("messages", "events", "candidates", "daily_stats"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('messages','events','candidates')")
    conn.commit()

    from app.browser import get_mock

    get_mock().reset()
    pipeline.reset_driver()


def speed_up() -> None:
    """把拟人化延迟压到接近零。

    注意：只改内存中的配置，不动 config/pacing.yaml。
    真实运行仍按原配置走随机间隔——那是防风控的关键，不能为了测试方便改掉。
    """
    pacing = config.settings.setdefault("pacing", {})
    pacing.update({
        "greet_interval_sec": [0, 0.01],
        "reply_interval_sec": [0, 0.01],
        "read_dwell_sec": [0, 0.01],
        "batch_rest_sec": [0, 0.01],
        "run_window": ["00:00", "23:59"],
    })


def main() -> int:
    _setup_console()
    print("=" * 68)
    print("AI 招聘助手 MVP · 端到端冒烟测试")
    print("=" * 68)
    print(f"模式：{config.mode}　岗位：{[j.name for j in config.active_jobs()]}")
    print()

    reset_data()
    speed_up()

    rounds = 14
    for i in range(1, rounds + 1):
        summary = pipeline.tick(once=True)
        s = summary.get("stats", {})
        print(
            f"[第 {i:02d} 轮] 回复 {summary.get('replied', 0)}　"
            f"打招呼 {summary.get('greeted', 0)}　"
            f"判定 {summary.get('judged', 0)}　"
            f"｜累计 触达 {s.get('greeted', 0)} 回复 {s.get('replied', 0)} "
            f"合格 {s.get('qualified', 0)} 约面 {s.get('invited', 0)} "
            f"转人工 {s.get('handover', 0)}"
        )
        if summary.get("stopped"):
            print(f"  ⚠ 触发熔断：{summary['stopped']}")

    print()
    print("-" * 68)
    print("候选人最终状态")
    print("-" * 68)
    labels = {
        "pending": "待筛选", "rejected_filter": "筛选未过", "prescreened": "待打招呼",
        "greeted": "已打招呼", "chatting": "沟通中", "qualified": "合格",
        "rejected": "不合格", "invited": "已约面", "handed_over": "待人工",
        "failed": "发送失败",
    }
    rows = db.list_candidates(limit=500)
    for c in sorted(rows, key=lambda x: x["id"]):
        state = labels.get(c["state"], c["state"])
        score = c["score"] if c["score"] is not None else "-"
        extra = c.get("invite_slot") or c.get("reject_reason") or ""
        print(f"  {c['id']:>2}  {c['name'] or '':<5} {c['job_id']:<12} {state:<8} 分={score!s:<4} {extra}")

    print()
    print("-" * 68)
    print("漏斗分布")
    print("-" * 68)
    for row in db.funnel_summary():
        print(f"  {labels.get(row['state'], row['state']):<10} {row['n']}")

    print()
    print("-" * 68)
    print("今日日报")
    print("-" * 68)
    from app.api.routes import daily_report

    report = daily_report()
    for k, v in report["summary"].items():
        print(f"  {k:<8} {v}")

    # 断言：关键分支必须都走到
    states = {c["state"] for c in rows}
    st = db.today_stats()
    checks = {
        "有筛选淘汰": "rejected_filter" in states or "rejected" in states,
        "有打招呼": int(st.get("greeted", 0)) > 0,
        "有对话": any(m for c in rows for m in db.get_messages(c["id"], limit=1)),
        "有判定结果": any(
            c.get("score_reason") and c["state"] in ("qualified", "rejected", "invited", "handed_over")
            for c in rows
        ),
        "有约面": "invited" in states,
    }
    print()
    print("-" * 68)
    print("关键分支覆盖")
    print("-" * 68)
    ok = True
    for name, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {name}")
        ok = ok and passed

    print()
    print("结果：" + ("全部通过" if ok else "存在未覆盖分支，见上方 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
