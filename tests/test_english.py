"""英语界面：设置页上的每一句话都必须有英语，不能露出中文。"""
import re
import unittest

import test_media_pipeline as fixtures


def load_app():
    return fixtures.app

CJK = re.compile("[\u3001\u3002\u3400-\u9fff\uff00-\uffef]")


def to_english(app, text):
    """和页面里的 toEnglish 同一套规则。"""
    core = text.strip()
    if core in app.EN_TEXT:
        return app.EN_TEXT[core]
    out = text
    for pattern, target in app.EN_PATTERNS:
        out = re.sub(pattern, re.sub(r"\$(\d)", r"\\\1", target), out)
    for key in sorted(app.EN_TEXT, key=len, reverse=True):
        out = out.replace(key, app.EN_TEXT[key])
    for zh, en in (("，", ", "), ("。", ". "), ("：", ": "), ("；", "; "), ("（", " ("), ("）", ")"), ("、", ", ")):
        out = out.replace(zh, en)
    return out


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


class EnglishCoverage(unittest.TestCase):
    def test_settings_page_is_fully_translated(self):
        app = load_app()
        view = app.settings_view()
        view.pop("fixed", None)  # 页面不显示这一项
        missing = sorted({text for text in strings(view)
                          if CJK.search(text) and CJK.search(to_english(app, text))})
        self.assertEqual(missing, [])

    def test_card_phrases_are_translated(self):
        app = load_app()
        for text in ("电视不支持此音轨，由 NAS 临时转换：音质下降，起播和快进变慢", "预计耗时 1.9 小时",
                     "成品仅含所选的 1 条音轨和 2 条字幕", "英语 · 环绕声 · 最高音质 · 全景声", "简体中文（推荐）",
                     "必须转换", "不用转换", "外挂 2 条", app.NO_FEATURE_REASON, app.MAKEMKV_OUTDATED):
            self.assertIsNone(CJK.search(to_english(app, text)), text)

    def test_patterns_are_valid_for_the_page(self):
        app = load_app()
        for pattern, _target in app.EN_PATTERNS:
            re.compile(pattern)
        self.assertNotIn("__EN_TEXT__", app.index())


if __name__ == "__main__":
    unittest.main()
