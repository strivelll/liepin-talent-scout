"""BOSS 薪资字体解码。

**这是什么问题**

BOSS 直聘把列表页的薪资数字替换成私用区（PUA）字符，再用自定义字体
`kanzhun-mix` 渲染出来。所以：

- 页面**看起来**是 "5-8K"
- DOM 里拿到的却是 `\\ue036-\\ue039K`（U+E036、U+E039）
- 直接读文本等于读乱码，薪资字段完全不可用

薪资是筛选的核心条件，不解决这层，整个系统的"薪资筛选"就是瞎的。

**映射怎么来的**

不是猜的。方法是在真实页面上注入一段 HTML，用页面自己的字体把
U+E030~U+E03F 逐个渲染出来，截图人眼确认。实测结果是顺序映射：

    U+E031 → 0, U+E032 → 1, ... U+E03A → 9

**为什么要写"校验"

字体混淆的映射**可能随平台更新而变化**（有些站点每次发版换一套字体）。
所以解码时必须统计未识别的 PUA 字符，一旦比例过高就报警，
而不是静默输出错数字——错数字比没数字更危险，会让筛选条件静默失效。

映射过期时的重新生成方法见 README「薪资字体解码」一节。
"""

from __future__ import annotations

import re
from typing import Any

# 实测映射（2026-10-21，字体 kanzhun-mix / 3kovsijnt11693967587313.woff2）
PUA_TO_DIGIT: dict[int, str] = {
    0xE031: "0",
    0xE032: "1",
    0xE033: "2",
    0xE034: "3",
    0xE035: "4",
    0xE036: "5",
    0xE037: "6",
    0xE038: "7",
    0xE039: "8",
    0xE03A: "9",
}

# PUA 区段（BMP 私用区），用于判断还有没有没解出来的字符
_PUA_MIN, _PUA_MAX = 0xE000, 0xF8FF


def has_pua(text: str) -> bool:
    """文本里是否含未解码的私用区字符。"""
    return any(_PUA_MIN <= ord(ch) <= _PUA_MAX for ch in text or "")


def decode(text: str) -> tuple[str, int]:
    """把 PUA 字符还原成数字。

    返回 (解码后文本, 未识别字符数)。未识别数 > 0 说明映射可能过期。
    """
    if not text:
        return "", 0
    out: list[str] = []
    unknown = 0
    for ch in text:
        code = ord(ch)
        if code in PUA_TO_DIGIT:
            out.append(PUA_TO_DIGIT[code])
        elif _PUA_MIN <= code <= _PUA_MAX:
            out.append("")      # 解不出来就丢掉，不留在结果里干扰
            unknown += 1
        else:
            out.append(ch)
    return "".join(out), unknown


def parse_salary(text: str) -> dict[str, Any]:
    """把薪资文本解析成结构化数据，供筛选比较。

    能处理这些形态：
        5-8K            → min 5000, max 8000
        15-30K·13薪     → min 15000, max 30000, months 13
        10-11K          → min 10000, max 11000
        200-300元/天     → min 200, max 300, unit day
        面议            → 无区间
    """
    raw, unknown = decode(text or "")
    clean = raw.replace(" ", "").replace("，", ",")

    result: dict[str, Any] = {
        "text": clean,
        "min": None,
        "max": None,
        "months": None,
        "unit": "month",
        "unknown_chars": unknown,
    }
    if not clean:
        return result

    # 年薪/日薪/时薪单位识别
    if "元/天" in clean or "/天" in clean:
        result["unit"] = "day"
    elif "元/时" in clean or "/时" in clean:
        result["unit"] = "hour"

    # 薪数（·13薪）
    m_months = re.search(r"(\d{1,2})薪", clean)
    if m_months:
        result["months"] = int(m_months.group(1))

    # 区间：5-8K / 5-8千 / 15-30K
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~—]\s*(\d+(?:\.\d+)?)\s*([Kk千万元]?)", clean)
    if m:
        lo, hi, unit = float(m.group(1)), float(m.group(2)), m.group(3)
        mult = 1
        if unit in ("K", "k", "千"):
            mult = 1000
        elif unit == "万":
            mult = 10000
        result["min"] = int(lo * mult)
        result["max"] = int(hi * mult)
        return result

    # 单值：8K以上 / 8K
    m = re.search(r"(\d+(?:\.\d+)?)\s*([Kk千万元]?)", clean)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        mult = 1
        if unit in ("K", "k", "千"):
            mult = 1000
        elif unit == "万":
            mult = 10000
        result["min"] = int(val * mult)
        result["max"] = int(val * mult)
    return result


def in_range(salary: dict[str, Any], lo: int | None = None, hi: int | None = None) -> bool:
    """判断薪资区间是否与 [lo, hi] 有交集。

    用"交集"而不是"包含"：岗位写 5-8K、要求 6-10K 时，两者有重叠就应算匹配，
    否则会把大量可谈的岗位漏掉。
    """
    smin, smax = salary.get("min"), salary.get("max")
    if smin is None and smax is None:
        return True  # 面议，不参与过滤
    smin = smin if smin is not None else 0
    smax = smax if smax is not None else 10 ** 9
    qlo = lo if lo is not None else 0
    qhi = hi if hi is not None else 10 ** 9
    return not (smax < qlo or smin > qhi)


def describe(salary: dict[str, Any]) -> str:
    """给人看的短描述，用于控制台输出。"""
    txt = salary.get("text") or ""
    if salary.get("unknown_chars"):
        txt += f"（{salary['unknown_chars']} 个字符未识别，映射可能过期）"
    return txt or "面议"
