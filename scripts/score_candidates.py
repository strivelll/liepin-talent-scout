"""给候选人打匹配分。

**为什么要单独一个脚本**
搜索只负责把卡片字段抓下来，不做判断。打分是另一件事：
需要岗位要求（JD）作为对照，还要调大模型，成本和耗时都不同步。
拆开之后，同一批候选人可以用不同 JD 反复打分，不用重搜。

**输出**：`data/liepin-probe/scores.json`
    {"张**|25岁": {"score": 78, "verdict": "聊", "reason": "…", "flags": [...]}, …}
控制台的 /api/candidates 会把它合并进候选人数据。

用法：
    python -m scripts.score_candidates --jd "Java 后端，5年以上，本科统招"
    python -m scripts.score_candidates --jd "…" --limit 20
    python -m scripts.score_candidates --jd "…" --rescore     # 重打已打过的
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CAND_DIR = ROOT / "data" / "liepin-probe"
SCORES = CAND_DIR / "scores.json"

SYS = "你是资深技术招聘顾问。只输出 JSON，不要任何解释文字。"

TEMPLATE = """岗位要求：
{jd}

候选人（猎聘简历卡片上的字段）：
学历：{edu}
年龄：{age}　工作年限：{years}
所在：{city}
求职期望：{expect}
技能：{skills}
最近经历：{recent}

请判断这个人是否值得主动联系，输出 JSON：
{{
  "score": 0 到 100 的整数,
  "verdict": "聊" 或 "观望" 或 "放弃",
  "reason": "一句话说明依据，30 字以内",
  "flags": ["疑点1", "疑点2"]
}}

评分要点：
- 方向是否真的匹配岗位（不是看着像）
- 年限与职级是否对得上
- 经历质量（公司层次、是否长期做同一方向）
- 明显疑点要扣分并写进 flags
只根据上面给的字段判断，缺失的信息不要编造。"""


def load_key() -> str:
    """取模型 Key。优先环境变量，其次项目根 .env。"""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("LLM_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def load_candidates() -> list[dict[str, Any]]:
    """和 console.py 用同一套取数逻辑：collected.json 优先，按 mtime 取新。"""
    best = None
    for name in ("collected.json", "cards.json"):
        f = CAND_DIR / name
        if not f.exists():
            continue
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            continue
        if not isinstance(rows, list) or not rows:
            continue
        m = f.stat().st_mtime
        if best is None or m > best[0]:
            best = (m, name, rows)
    return best[2] if best else []


def card_key(r: dict[str, Any]) -> str:
    """候选人没有稳定 ID，用 姓名+年龄 做键。同名同岁的概率极低，够用。"""
    return f"{r.get('姓名', '')}|{r.get('年龄', '')}"


def brief(r: dict[str, Any]) -> dict[str, str]:
    works = r.get("工作经历") or []
    edus = r.get("教育经历") or []
    return {
        "edu": (edus[0] if edus else "未标注")[:60],
        "age": r.get("年龄") or "未标注",
        "years": r.get("年限") or "未标注",
        "city": r.get("地区") or "未标注",
        "expect": (r.get("期望") or "未标注")[:40],
        "skills": " / ".join((r.get("技能") or [])[:8]) or "未标注",
        "recent": (works[0] if works else "未标注")[:70],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd", required=True, help="岗位要求，越具体越准")
    ap.add_argument("--limit", type=int, default=0, help="最多打几个人，0 = 全部")
    ap.add_argument("--rescore", action="store_true", help="重打已经打过的")
    args = ap.parse_args()

    key = load_key()
    if not key:
        print("✗ 没找到 LLM_API_KEY")
        print("  请设置环境变量，或在项目根目录建 .env 写上 LLM_API_KEY=sk-xxx")
        return 1
    os.environ["LLM_API_KEY"] = key

    from app.llm import llm

    if not llm.available:
        print("✗ 模型不可用，检查 config/settings.yaml 的 llm 段")
        return 1

    rows = load_candidates()
    if not rows:
        print("✗ 没有候选人数据，先跑搜索（控制台第 2 步或 probe_liepin --search）")
        return 1

    scores: dict[str, Any] = {}
    if SCORES.exists() and not args.rescore:
        try:
            scores = json.loads(SCORES.read_text(encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            scores = {}

    todo = [r for r in rows if args.rescore or card_key(r) not in scores]
    if args.limit:
        todo = todo[: args.limit]

    print(f"候选人 {len(rows)} 人，已有评分 {len(scores)} 人，本轮打 {len(todo)} 人")
    print(f"岗位：{args.jd[:60]}")
    print()

    if not todo:
        print("没有需要打分的（要重打请加 --rescore）")
        return 0

    ok = 0
    t0 = time.time()
    for i, r in enumerate(todo, 1):
        b = brief(r)
        try:
            res = llm._chat(SYS, TEMPLATE.format(jd=args.jd, **b))
            data = llm._extract_json(res.text) or {}
            s = int(data.get("score", 0) or 0)
            scores[card_key(r)] = {
                "score": s,
                "verdict": str(data.get("verdict", "") or ""),
                "reason": str(data.get("reason", "") or "")[:40],
                "flags": data.get("flags") or [],
                "jd": args.jd[:40],
            }
            ok += 1
            bar = "█" * max(1, s // 10)
            print(f"  [{i}/{len(todo)}] {r.get('姓名','?'):<6} {s:>3} {bar} {data.get('verdict','')}  {scores[card_key(r)]['reason']}")
        except Exception as exc:                  # noqa: BLE001
            print(f"  [{i}/{len(todo)}] {r.get('姓名','?')} 失败: {str(exc)[:50]}")
        time.sleep(0.2)

    SCORES.write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"✓ 完成 {ok} 人，用时 {time.time() - t0:.0f}s，累计 {len(scores)} 条")
    print(f"  已保存：{SCORES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
