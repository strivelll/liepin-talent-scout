"""在发布岗位：展开、抓取、去重、精确匹配的回归测试（先红灯）。

客户反馈：下拉只能选到默认展示的岗位，藏在「展开更多职位」里的选不到。
根因链：
1. 折叠区 chip 被父容器 overflow:hidden 裁剪，按坐标点不到
2. 抓列表时把 527 字的外层容器也当成岗位
3. 去重键取前 40 字，把同岗位不同薪资合并掉了
4. 下拉 option.value 只存城市，同城多个岗位无法区分
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import console as con  # noqa: E402
from scripts import send_liepin as sl  # noqa: E402

CHIPS = [
    "AI Agent产…（阿里巴巴(中国)有限… | 杭州-滨江区 | 30k-50k）",
    "AI Agent产…（阿里巴巴(中国)有限… | 北京-朝阳区 | 30k-50k）",
    "AI Agent产…（阿里巴巴(中国)有限… | 杭州 | 30k-50k）",
    "AI Agent产…（阿里巴巴(中国)有限… | 北京-朝阳区 | 35k-50k）",
    "公共云解决方…（阿里巴巴(中国)有限… | 西安 | 35k-50k）",
    "机械工程师（迈胜医疗设备有限公司 | 广州-黄埔区 | 20k-35k）",
    "硬件负责人（深圳硅羽科技有限公司 | 深圳 | 30k-60k）",
    "AI软件架构师（奥锐杰(深圳)科技有… | 深圳 | 30k-60k）",
]
WRAPPER = " ".join(CHIPS)  # 外层 .ul-box 的 innerText，527 字


def _cand(t: str, children: int = 1, y: int = 300) -> dict:
    return {"text": t, "children": children, "x": 400, "y": y, "w": 300, "h": 20}


class TestPickJobChips(unittest.TestCase):
    """只认真正的 chip，排除外层容器。"""

    def test_wrapper_excluded(self):
        got = sl.pick_job_chips([_cand(WRAPPER, children=1), _cand(CHIPS[0])])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], CHIPS[0])

    def test_all_chips_kept(self):
        got = sl.pick_job_chips([_cand(t, y=260 + i * 32) for i, t in enumerate(CHIPS)])
        self.assertEqual(len(got), len(CHIPS))
        self.assertEqual([c["text"] for c in got], CHIPS)

    def test_dedup_uses_full_text(self):
        # 外层 .ul-box 的全部文本以第一个 chip 开头 → 前 40 字完全相同，
        # 旧去重键 c[:40] 会把真正的 chip 误删，只留下脏容器
        self.assertEqual(WRAPPER[:40], CHIPS[0][:40])
        got = sl.pick_job_chips([_cand(WRAPPER), _cand(CHIPS[0]), _cand(CHIPS[1])])
        self.assertEqual([c["text"] for c in got], [CHIPS[0], CHIPS[1]])

    def test_exact_duplicate_removed(self):
        got = sl.pick_job_chips([_cand(CHIPS[0]), _cand(CHIPS[0])])
        self.assertEqual(len(got), 1)

    def test_too_many_children_excluded(self):
        got = sl.pick_job_chips([_cand(CHIPS[0], children=9)])
        self.assertEqual(got, [])


class TestMatchJobChip(unittest.TestCase):
    """完整 label 精确命中；旧的城市短值仍能回退匹配。"""

    def test_exact_label_wins(self):
        idx = sl.match_job_chip(CHIPS, "AI软件架构师（奥锐杰(深圳)科技有… | 深圳 | 30k-60k）")
        self.assertEqual(idx, 7)

    def test_city_fallback(self):
        self.assertEqual(sl.match_job_chip(CHIPS, "西安"), 4)

    def test_not_found(self):
        self.assertEqual(sl.match_job_chip(CHIPS, "不存在的岗位"), -1)

    def test_empty_keyword(self):
        self.assertEqual(sl.match_job_chip(CHIPS, ""), -1)


class TestExpandControl(unittest.TestCase):
    """展开/收起控件识别：只有「展开」状态才需要点。"""

    def test_expand_yes(self):
        self.assertTrue(sl.is_expand_control("展开更多职位"))
        self.assertTrue(sl.is_expand_control(" 展开 更多职位 "))

    def test_collapse_no(self):
        self.assertFalse(sl.is_expand_control("收起更多职位"))
        self.assertFalse(sl.is_expand_control("更多"))
        self.assertFalse(sl.is_expand_control(""))


class TestNormalizeJobs(unittest.TestCase):
    """缓存里的岗位必须带唯一 key（下拉 option.value 用它）。"""

    def test_string_cache_gets_unique_key(self):
        rows = con._normalize_jobs(CHIPS[:4])
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r["key"] for r in rows}), 4)
        self.assertTrue(all(r["city"] for r in rows))

    def test_dict_without_key_backfilled(self):
        rows = con._normalize_jobs([{"label": CHIPS[0], "name": "AI Agent产…", "city": "杭州"}])
        self.assertEqual(rows[0]["key"], CHIPS[0])

    def test_dict_with_key_preserved(self):
        rows = con._normalize_jobs([{"label": "x", "key": "UNIQUE", "name": "n", "city": "c"}])
        self.assertEqual(rows[0]["key"], "UNIQUE")


CHIP_CANDS = [{"text": t, "children": 1, "x": 400, "y": 300, "w": 300, "h": 20}
              for t in CHIPS]


class _FakeMouse:
    def __init__(self):
        self.clicks = 0

    def click(self, *_a, **_kw):
        self.clicks += 1


class _JobClickPage:
    """假页面：只响应 JS 点击/状态检查，坐标点击会被计数。"""

    def __init__(self, active: bool = True):
        self.active = active
        self.mouse = _FakeMouse()

    def evaluate(self, _script, _arg=None):
        return {"ok": True, "found": True, "active": self.active,
                "cls": "job active" if self.active else "job"}


class TestJobChipClick(unittest.TestCase):
    """点岗位必须用 JS click 并校验 li.job.active，不能依赖坐标。

    实测：展开后 .ul-box 会折回 24px，折叠区 chip 的 y=466 落在裁剪区外，
    mouse.click 点不到（li.class 仍是 job）；span.info.click() 则稳定变 job active。
    """

    def test_is_job_active(self):
        self.assertTrue(sl.is_job_active("job active"))
        self.assertTrue(sl.is_job_active("active"))
        self.assertFalse(sl.is_job_active("job"))
        self.assertFalse(sl.is_job_active(""))

    @patch.object(sl, "collect_job_chips", return_value=CHIP_CANDS)
    @patch.object(sl.time, "sleep")
    def test_js_click_no_coordinate_click(self, _sleep, _coll):
        pg = _JobClickPage(active=True)
        ok, txt = sl.click_posted_job_chip(pg, CHIPS[-1])
        self.assertTrue(ok)
        self.assertIn("AI软件架构师", txt)
        self.assertEqual(pg.mouse.clicks, 0, "不能依赖坐标点击")

    @patch.object(sl, "collect_job_chips", return_value=CHIP_CANDS)
    @patch.object(sl.time, "sleep")
    def test_not_active_reports_failure(self, _sleep, _coll):
        pg = _JobClickPage(active=False)
        ok, msg = sl.click_posted_job_chip(pg, CHIPS[-1])
        self.assertFalse(ok)
        self.assertIn("未选中", msg)

    @patch.object(sl, "collect_job_chips", return_value=[])
    @patch.object(sl.time, "sleep")
    def test_no_chips_reports_count(self, _sleep, _coll):
        pg = _JobClickPage(active=True)
        ok, msg = sl.click_posted_job_chip(pg, "任意岗位")
        self.assertFalse(ok)
        self.assertIn("没找到岗位 chip", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
