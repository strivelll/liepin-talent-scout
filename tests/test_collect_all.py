"""collect_all 三项遗留缺陷的回归测试（先红灯，再实现）。

1. 采集前必须判断「对方是否已回复联系方式」（客户硬要求）
2. 采集后必须写入人才库，且不能把 library_* 这类非简历目录误入库
3. PDF 必须带头像 + 姓名等顶部信息，且对网页文本做转义
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import talent as talent_lib  # noqa: E402
from scripts import collect_all as ca  # noqa: E402


class TestContactReply(unittest.TestCase):
    """只认对方真发了联系方式，我方索要话术不能算。"""

    def test_platform_phone_tag(self):
        r = ca.contact_reply("刘先生 数字化技术项目经理 11月2日 [手机号]")
        self.assertTrue(r["ok"])
        self.assertTrue(r["phone"])

    def test_platform_wechat_tag(self):
        r = ca.contact_reply("王女士 产品总监 昨天 [微信号]")
        self.assertTrue(r["ok"])
        self.assertTrue(r["wechat"])

    def test_raw_phone_number(self):
        self.assertTrue(ca.contact_reply("我的手机号是 13800138000，方便联系")["ok"])

    def test_wxid(self):
        self.assertTrue(ca.contact_reply("加我微信吧 wxid_abc12345")["ok"])

    def test_our_request_is_not_a_reply(self):
        for t in (
            "陈先生 架构师 17:20 [未读]为了方便联系，你能把微信号发我吗？",
            "雷示例 Java工程师 13:29 [未读]您好，我们Java后端岗位，方便回复手机号或微信吗？",
            "李女士 Java开发 昨天 [未读]您好，方便的话回复一下您的手机号或微信，我把岗位JD发您看看？",
            "郭示例 解决方案架构师 10:46 您好，我对AI有很高的热情，希望能详谈。",
            "",
            None,
        ):
            self.assertFalse(ca.contact_reply(t)["ok"], t)

    def test_tags_are_reported(self):
        r = ca.contact_reply("张三 今天 [手机号] [微信号]")
        self.assertEqual(set(r["tags"]), {"[手机号]", "[微信号]"})


class TestExtractBasics(unittest.TestCase):
    """从侧栏简历文本里恢复能恢复的字段，取不到不编造。"""

    TEXT = ("求职意向\n深度学习\n·北京\n60-120k·18薪\n人工智能\n"
            "工作经历\n北京宇信科技集团股份有限公司\n架构师\n2025.05-至今\n"
            "教育经历\n重庆大学\n本科 ·计算机科学与技术\n2004.09—2008.06")

    def test_fields(self):
        b = ca.extract_basics(self.TEXT)
        self.assertEqual(b["expect"], "深度学习")
        self.assertEqual(b["city"], "北京")
        self.assertEqual(b["company"], "北京宇信科技集团股份有限公司")
        self.assertEqual(b["edu"], "本科")

    def test_empty_text(self):
        b = ca.extract_basics("")
        self.assertEqual(b, {"expect": "", "city": "", "company": "", "edu": ""})


class TestBuildHeader(unittest.TestCase):
    """PDF 顶部信息条：头像 + 姓名 + 岗位 + 联系方式标记。"""

    def test_contains_avatar_and_name(self):
        h = ca.build_header("陈先生", "架构师", "data:image/png;base64,AAAA",
                            {"expect": "深度学习", "city": "北京",
                             "company": "宇信科技", "edu": "本科"},
                            {"ok": True, "phone": True, "wechat": False,
                             "tags": ["[手机号]"]})
        self.assertIn("data:image/png;base64,AAAA", h)
        self.assertIn("陈先生", h)
        self.assertIn("架构师", h)
        self.assertIn("宇信科技", h)
        self.assertIn("已回复联系方式", h)

    def test_escapes_web_text(self):
        h = ca.build_header("<script>alert(1)</script>", "A&B", "",
                            {"expect": "", "city": "", "company": "", "edu": ""},
                            {"ok": False, "phone": False, "wechat": False, "tags": []})
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("A&amp;B", h)

    def test_no_avatar_still_renders(self):
        h = ca.build_header("张三", "", "", {"expect": "", "city": "",
                                            "company": "", "edu": ""},
                            {"ok": False, "phone": False, "wechat": False, "tags": []})
        self.assertIn("张三", h)


class TestAvatarMime(unittest.TestCase):
    """猎聘头像 URL 是 .png，内容却常是 JPEG；必须按魔数判 mime。"""

    def test_jpeg(self):
        self.assertEqual(ca.avatar_mime(b"\xff\xd8\xff\xe0\x00\x10JFIF"), "image/jpeg")

    def test_png(self):
        self.assertEqual(ca.avatar_mime(b"\x89PNG\r\n\x1a\n\x00\x00"), "image/png")

    def test_gif_webp(self):
        self.assertEqual(ca.avatar_mime(b"GIF89a123"), "image/gif")
        self.assertEqual(ca.avatar_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")

    def test_unknown_defaults_to_jpeg(self):
        self.assertEqual(ca.avatar_mime(b""), "image/jpeg")
        self.assertEqual(ca.avatar_mime(b"garbage"), "image/jpeg")


class TestResumeDirFilter(unittest.TestCase):
    """只有 {姓名}_{YYYYMMDD_HHMMSS} 才是简历目录，library_* 不能进人才库。"""

    def test_valid_dirs(self):
        for n in ("陈先生_20261105_172140", "屈示例_20261104_115029", "A_20260101_000000"):
            self.assertTrue(talent_lib.is_resume_dir(n), n)

    def test_invalid_dirs(self):
        for n in ("library_html", "library_meta", "library", "library_export",
                  "唐示例_692b776e8b", "随便一个目录", ""):
            self.assertFalse(talent_lib.is_resume_dir(n), n)


class TestImportSkipsNonResumeDirs(unittest.TestCase):
    """真实跑一遍 import_dirs：脏目录不产生虚假人才记录。"""

    def test_skip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            resumes = root / "resumes"
            resumes.mkdir()
            good = resumes / "测试甲_20261105_172140"
            good.mkdir()
            (good / "resume.txt").write_text("求职意向\n产品经理\n·北京\n工作经历\n某公司\n"
                                            "产品经理\n2020.01-至今\n教育经历\n某大学\n本科 ·计算机",
                                            encoding="utf-8")
            (good / "resume.json").write_text('{"_联系方式已回复": true}', encoding="utf-8")
            bad = resumes / "library_meta"
            bad.mkdir()
            (bad / "resume.txt").write_text("这不是一份简历 " * 30, encoding="utf-8")
            # 未筛选的历史产物：目录名合法但没有联系方式标记，不该入库
            old = resumes / "测试乙_20261105_172140"
            old.mkdir()
            (old / "resume.txt").write_text("求职意向\nJava\n·北京\n工作经历\n某公司\n"
                                           "工程师\n2020.01-至今\n教育经历\n某大学\n本科 ·计算机",
                                           encoding="utf-8")
            (old / "resume.json").write_text('{"_姓名": "测试乙"}', encoding="utf-8")
            with patch.object(talent_lib, "RESUME_DIR", resumes), \
                 patch.object(talent_lib, "DB_PATH", root / "t.db"), \
                 patch.object(talent_lib, "ROOT", root):
                st = talent_lib.import_dirs()
                names = [r["name"] for r in talent_lib.list_talent()]
            self.assertEqual(st["新增"], 1)
            self.assertEqual(names, ["测试甲"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
