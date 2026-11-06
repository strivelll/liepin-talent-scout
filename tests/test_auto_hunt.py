"""auto_hunt 一键寻访的回归测试（先写失败测试，再实现）。

覆盖 2026-11-05 遗留的 5 个真实缺陷：
1. 「继续沟通」卡片被当成新候选人 → 等不到弹窗 → 开聊按钮异常
2. 弹窗按钮只查 button/a，漏 span/div → 找不到「不选择职位开聊」
3. 卡片字段靠整段 innerText 截取 → 姓名被「名片简历」污染、地区全空
4. 脱敏名无法定位沟通页会话 → 用置顶会话 + 姓氏校验
5. 失败分支 continue 跳过进度落盘 → 界面卡在 running/done:0
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.console import build_argv  # noqa: E402
from scripts import auto_hunt as ah  # noqa: E402


class TestCleanName(unittest.TestCase):
    """姓名不能被状态词/标签词污染（progress.json 里出现过「名片简历」当姓名）。"""

    def test_badge_suffix_removed(self):
        self.assertEqual(ah.clean_name("张**\n名片简历"), "张**")

    def test_badge_only_is_not_a_name(self):
        self.assertEqual(ah.clean_name("名片简历"), "")

    def test_status_words_are_not_names(self):
        for w in ("在线", "今天活跃", "3天内活跃", "30天内活跃", "", None):
            self.assertEqual(ah.clean_name(w), "", w)

    def test_real_name_kept(self):
        self.assertEqual(ah.clean_name("陈先生"), "陈先生")
        self.assertEqual(ah.clean_name("高示例"), "高示例")

    def test_normalized_whitespace(self):
        self.assertEqual(ah.clean_name("  李**  名片简历 "), "李**")


class TestNormalizeCard(unittest.TestCase):
    """JS 只采集 DOM 字段，清洗逻辑放 Python，保证可测。"""

    def _raw(self, **kw):
        base = {
            "name_raw": "张**\n名片简历", "age": "43岁", "years": "--",
            "edu": "", "city": "北京", "expect": "北京机械维修/保养",
            "raw": "张** 名片简历 43岁 -- 北京 求职期望： 北京机械维修/保养 立即沟通",
            "buttons": ["立即沟通"],
        }
        base.update(kw)
        return base

    def test_fields_are_clean(self):
        r = ah.normalize_card(self._raw())
        self.assertEqual(r["姓名"], "张**")
        self.assertEqual(r["年龄"], "43岁")
        self.assertEqual(r["年限"], "")       # "--" 不能当工作年限
        self.assertEqual(r["学历"], "")
        self.assertEqual(r["地区"], "北京")   # 旧实现全空
        self.assertEqual(r["期望"], "北京机械维修/保养")  # 旧实现是整段脏文本
        self.assertEqual(r["button"], "new")

    def test_continue_chat_classified_as_existing(self):
        r = ah.normalize_card(self._raw(name_raw="陈先生", buttons=["继续沟通"]))
        self.assertEqual(r["button"], "existing")

    def test_years_kept_when_present(self):
        r = ah.normalize_card(self._raw(years="工作18年", edu="本科"))
        self.assertEqual(r["年限"], "工作18年")
        self.assertEqual(r["学历"], "本科")

    def test_bad_name_row_is_skipped(self):
        self.assertIsNone(ah.normalize_card(self._raw(name_raw="名片简历")))

    def test_raw_preserved_for_scoring(self):
        r = ah.normalize_card(self._raw())
        self.assertIn("求职期望", r["raw"])


class TestDedupAndKeys(unittest.TestCase):
    """脱敏姓名重复率高，去重/定位必须用组合键。"""

    def _row(self, name, age, expect, city="北京"):
        return {"姓名": name, "年龄": age, "地区": city, "期望": expect, "raw": "x" * 50}

    def test_same_name_age_different_expect_are_two_people(self):
        rows = [self._row("王**", "32岁", "北京Java开发"),
                self._row("王**", "32岁", "上海产品经理")]
        self.assertEqual(len(ah.dedup(rows)), 2)

    def test_exact_duplicate_removed(self):
        r = self._row("王**", "32岁", "北京Java开发")
        self.assertEqual(len(ah.dedup([r, dict(r)])), 1)

    def test_card_match_tokens_are_specific(self):
        r = self._row("高**", "40岁", "北京技术/研发总监")
        t = ah.match_tokens(r)
        self.assertIn("高**", t)
        self.assertIn("40岁", t)
        self.assertTrue(any("北京技术" in x for x in t))


class TestChatButtonDecision(unittest.TestCase):
    """弹窗按钮决策：span/div 也必须能命中（旧版只查 button/a）。"""

    def test_chat_now_preferred(self):
        entries = [{"tag": "BUTTON", "text": "不选择职位开聊"},
                   {"tag": "BUTTON", "text": "立即开聊"}]
        self.assertEqual(ah.choose_chat_button(entries), ("chat-now", 1))

    def test_span_button_is_reachable(self):
        entries = [{"tag": "SPAN", "text": "不选择职位开聊"}]
        self.assertEqual(ah.choose_chat_button(entries), ("no-job", 0))

    def test_div_button_is_reachable(self):
        entries = [{"tag": "DIV", "text": "不选择职位开聊"}]
        self.assertEqual(ah.choose_chat_button(entries), ("no-job", 0))

    def test_whitespace_normalized(self):
        entries = [{"tag": "A", "text": " 立即 开聊 "}]
        self.assertEqual(ah.choose_chat_button(entries), ("chat-now", 0))

    def test_prefers_real_button_over_wrapper_div(self):
        entries = [{"tag": "DIV", "text": "立即开聊"},
                   {"tag": "BUTTON", "text": "立即开聊"}]
        self.assertEqual(ah.choose_chat_button(entries), ("chat-now", 1))

    def test_no_button_returns_none(self):
        self.assertIsNone(ah.choose_chat_button([{"tag": "BUTTON", "text": "取 消"}]))
        self.assertIsNone(ah.choose_chat_button([]))

    def test_classify_card_button(self):
        self.assertEqual(ah.classify_chat_button("立即沟通"), "new")
        self.assertEqual(ah.classify_chat_button("继续沟通"), "existing")
        self.assertIsNone(ah.classify_chat_button(""))
        self.assertIsNone(ah.classify_chat_button("立即开聊"))


class TestConversationTargeting(unittest.TestCase):
    """沟通页是真名、搜索页是脱敏名：只认置顶新会话 + 姓氏校验。"""

    def test_surname_matches_first_conv(self):
        self.assertEqual(ah.pick_conv_index(["高示例", "雷示例", "李女士"], "高**"), 0)

    def test_real_name_surname(self):
        self.assertEqual(ah.pick_conv_index(["陈先生", "高示例"], "陈先生"), 0)

    def test_mismatch_refuses_to_send(self):
        self.assertIsNone(ah.pick_conv_index(["雷示例", "李女士"], "高**"))

    def test_empty_list_refuses_to_send(self):
        self.assertIsNone(ah.pick_conv_index([], "高**"))
        self.assertIsNone(ah.pick_conv_index(["高示例"], ""))


class TestProgressAlwaysRecorded(unittest.TestCase):
    """任何失败分支都必须写进度，否则控制台永远显示 running/done:0。"""

    def setUp(self):
        self.p = {"status": "running", "done": 0, "total": 2, "rows": []}

    def test_failure_item_is_recorded(self):
        item = {"姓名": "高**", "score": 55, "沟通": "开聊按钮异常", "话术": "", "索要": {}}
        ah.record_contact_progress(self.p, 1, item)
        self.assertEqual(self.p["done"], 1)
        self.assertEqual(self.p["rows"][0]["沟通"], "开聊按钮异常")

    def test_success_item_is_recorded(self):
        item = {"姓名": "高**", "score": 55, "沟通": "已建会话", "话术": "已发送", "索要": {}}
        ah.record_contact_progress(self.p, 2, item)
        self.assertEqual(self.p["done"], 2)
        self.assertEqual(self.p["rows"][0]["话术"], "已发送")

    def test_final_status(self):
        ah.finish_contact_progress(self.p, blocked=False)
        self.assertEqual(self.p["status"], "done")
        ah.finish_contact_progress(self.p, blocked=True)
        self.assertEqual(self.p["status"], "stopped")


class _FakeBtn:
    def evaluate(self, _script):
        return True


class _FakeCard:
    def query_selector_all(self, sel):
        return [_FakeBtn()] if sel == "button" else []


class _FakePage:
    """通用假页面：记录 goto、可配置弹窗是否出现。"""

    def __init__(self, modal: bool = False):
        self.modal = modal
        self.modal_checks = 0
        self.calls: list[str] = []

    def goto(self, *_a, **_kw):
        self.calls.append("goto")
        return None

    def evaluate(self, script, _arg=None):
        self.calls.append("evaluate")
        if "!!document.querySelector('.ant-modal')" in str(script):
            self.modal_checks += 1
            return self.modal
        return {}

    def query_selector_all(self, sel):
        return [_FakeCard()] if sel == ".detail-resume-card-wrap" else []

    def query_selector(self, sel):
        return None


class _FakeGuard:
    def after_nav(self):
        pass


class TestContactOneIntegration(unittest.TestCase):
    """假页面跑单人链路：验证「继续沟通」分支与失败回写，不碰真实浏览器。"""

    @staticmethod
    def _args():
        return argparse.Namespace(query="AI", posted_job="", jd="", text="",
                                  greet="", pages=1, limit=1, what="", dry=False)

    @staticmethod
    def _row():
        return {"姓名": "陈先生", "年龄": "41岁", "年限": "工作18年", "学历": "本科",
                "地区": "北京", "期望": "北京自然语言处理(NLP)", "raw": "x" * 50,
                "button": "existing", "score": 35, "verdict": "放弃", "reason": "方向不符",
                "card_index": 0, "page_no": 1}

    @patch.object(ah, "click_card_by_index", return_value=("existing", "fake"))
    @patch.object(ah.time, "sleep")
    def test_existing_conv_does_not_wait_for_modal(self, _sleep, _card):
        pg = _FakePage(modal=True)
        item = ah.contact_one(pg, _FakeGuard(), self._args(), self._row(), [])
        self.assertEqual(item["沟通"], "继续沟通(已有会话)")
        self.assertEqual(pg.modal_checks, 0, "已有会话不应再等开聊弹窗")

    @patch.object(ah, "click_card_by_index", return_value=("new", "fake"))
    @patch.object(ah.time, "sleep")
    def test_new_conv_without_modal_reports_reason(self, _sleep, _card):
        pg = _FakePage(modal=False)
        item = ah.contact_one(pg, _FakeGuard(), self._args(), self._row(), [])
        self.assertEqual(item["沟通"], "弹窗未出现")
        self.assertEqual(pg.modal_checks, 10)

    @patch.object(ah, "click_card_by_index", return_value=("new", "fake"))
    @patch.object(ah.time, "sleep")
    def test_failure_item_is_still_written_to_progress(self, _sleep, _card):
        pg = _FakePage(modal=False)
        item = ah.contact_one(pg, _FakeGuard(), self._args(), self._row(), [])
        progress = {"status": "running", "done": 0, "total": 1, "rows": []}
        ah.record_contact_progress(progress, 1, item)
        ah.finish_contact_progress(progress, blocked=False)
        self.assertEqual(progress["status"], "done")
        self.assertEqual(progress["done"], 1)
        self.assertEqual(progress["rows"][0]["沟通"], "弹窗未出现")


class _NoNavPage(_FakePage):
    """沟通阶段用例：弹窗默认出现，同时记录是否有 goto。"""

    def __init__(self):
        super().__init__(modal=True)


class TestContactStaysOnResultPage(unittest.TestCase):
    """客户要求的流程：一次搜索 → 打分 → Top N 直接在同一结果页沟通。

    不允许沟通阶段再 goto / do_search / 重新选岗位——列表一变就找不到之前的人。
    """

    @staticmethod
    def _row():
        return {"姓名": "陈先生", "年龄": "41岁", "年限": "工作18年", "学历": "本科",
                "地区": "北京", "期望": "北京自然语言处理(NLP)", "raw": "x" * 50,
                "button": "new", "score": 35, "verdict": "观望", "reason": "方向不符",
                "card_index": 0, "page_no": 1}

    @staticmethod
    def _args():
        return argparse.Namespace(query="AI", posted_job="杭州", jd="", text="",
                                  greet="", pages=1, limit=1, what="", dry=False)

    def test_no_goto_no_research_no_chip(self):
        pg = _NoNavPage()
        with patch.object(ah, "do_search", return_value=True) as m_search, \
             patch.object(ah, "click_posted_job_chip", return_value=(True, "x")) as m_chip, \
             patch.object(ah, "click_card_by_index", create=True,
                          return_value=("new", "ok")) as m_card, \
             patch.object(ah, "click_chat_now", return_value=(True, "立即开聊")), \
             patch.object(ah.time, "sleep"):
            item = ah.contact_one(pg, _FakeGuard(), self._args(), self._row(), [])
        self.assertNotIn("goto", pg.calls, "沟通阶段不能重新导航到搜索页")
        m_search.assert_not_called()
        m_chip.assert_not_called()
        m_card.assert_called_once()
        self.assertEqual(item["沟通"], "已建会话")

    def test_card_index_survives_sorting(self):
        rows = [{"姓名": "甲", "score": 10}, {"姓名": "乙", "score": 90},
                {"姓名": "丙", "score": 50}]
        ah.attach_card_index(rows, page_no=1)
        rows.sort(key=lambda x: -x["score"])
        self.assertEqual([r["card_index"] for r in rows], [1, 2, 0])
        self.assertTrue(all(r["page_no"] == 1 for r in rows))

    def test_card_text_must_still_match(self):
        tokens = ah.match_tokens({"姓名": "陈先生", "年龄": "41岁",
                                  "期望": "北京自然语言处理(NLP)"})
        self.assertTrue(ah.card_still_matches("在线 陈先生 41岁 北京 求职期望： 北京自然语言处理(NLP)", tokens))
        self.assertFalse(ah.card_still_matches("在线 王** 30岁 上海 求职期望： 产品经理", tokens))


class TestCliContract(unittest.TestCase):
    """前端 → build_argv → CLI：每个参数都必须被 argparse 接受（踩过 --city / --query）。"""

    def test_hunt_argv_parses(self):
        params = {"query": "AI", "jd": "AI Agent 产品经理（杭州）", "text": "你好，方便聊聊吗",
                  "posted_job": "杭州", "greet": "GIS", "what": "phone,wechat",
                  "limit": 3, "pages": 2, "dry": True}
        argv = build_argv("hunt", params)
        self.assertEqual(argv[0], "scripts.auto_hunt")
        parsed = ah.make_parser().parse_args(argv[1:])
        self.assertEqual(parsed.query, "AI")
        self.assertEqual(parsed.posted_job, "杭州")
        self.assertEqual(parsed.limit, 3)
        self.assertEqual(parsed.pages, 2)
        self.assertTrue(parsed.dry)

    def test_no_unknown_flags(self):
        argv = build_argv("hunt", {"query": "AI", "dry": False})
        for flag in argv[1:]:
            if flag.startswith("--"):
                self.assertIn(flag, ah.make_parser()._option_string_actions,
                              f"CLI 未定义参数 {flag}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
