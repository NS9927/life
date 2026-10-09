"""去格式化（`format_guard`）测试：Markdown / 列表 / 表格拍平。

两条主线：

1. **只删标记、绝不丢内容**——纯文本逐字不变；代码块内容不被二次加工；
   列表拍平保留原有标点。
2. **拍平后段数下降**——真机 7~8 段的编号清单，去格式化后只剩 2~3 段。

接线部分复用 tests/test_segmented_jitter.py 的假件（同一套 FakeEvent/FakeContext）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "life"))

from tests._astrbot_stub import FakePlain, install  # noqa: E402

LOGGER = install()  # 必须在 import main 之前（幂等）

from life.core import format_guard as fg  # noqa: E402
from life.core import segmented  # noqa: E402
from life import main as plugin_main  # noqa: E402
from life import webapi  # noqa: E402
from tests.test_segmented_jitter import (  # noqa: E402
    FakeContext,
    FakeEvent,
    jitter_conf,
    make_config,
)

CFG = fg.FormatGuardConfig(enable=True)
NO_MARKDOWN = fg.FormatGuardConfig(enable=True, strip_markdown=False)
NO_LISTS = fg.FormatGuardConfig(enable=True, flatten_lists=False)


def clean(text: str, config: fg.FormatGuardConfig = CFG) -> str:
    return fg.clean(text, config)[0]


# ======================================================================
# 1. 行内 Markdown
# ======================================================================
class TestInlineMarkdown(unittest.TestCase):
    def test_bold(self):
        self.assertEqual(clean("**群聊版：** 今天群里在聊啥"), "群聊版： 今天群里在聊啥")
        self.assertEqual(clean("__加粗__"), "加粗")

    def test_italic(self):
        self.assertEqual(clean("*斜体*"), "斜体")
        self.assertEqual(clean("_斜体_"), "斜体")

    def test_italic_does_not_eat_snake_case_or_math(self):
        # 变量名 foo_bar_baz、乘法 3*4*5 都不能被动
        for text in ("foo_bar_baz", "3*4*5", "a_b_c = 1", "snake_case_name"):
            self.assertEqual(clean(text), text, msg=text)

    def test_strike(self):
        self.assertEqual(clean("~~删掉这句~~"), "删掉这句")

    def test_inline_code(self):
        self.assertEqual(clean("用 `debug tooltip` 看"), "用 debug tooltip 看")
        self.assertEqual(clean("``双反引号``"), "双反引号")

    def test_heading(self):
        self.assertEqual(clean("# 结论\n## 第二级\n正文"), "结论\n第二级\n正文")
        self.assertEqual(clean("####### 七个井号不是标题"), "####### 七个井号不是标题")

    def test_quote(self):
        self.assertEqual(clean("> 引用一句话"), "引用一句话")
        self.assertEqual(clean(">> 嵌套引用"), "嵌套引用")

    def test_horizontal_rule_dropped(self):
        self.assertEqual(clean("上一段\n\n---\n\n下一段"), "上一段\n\n\n下一段")
        self.assertEqual(clean("***"), "")

    def test_combined_bold_italic(self):
        self.assertEqual(clean("***又粗又斜***"), "又粗又斜")

    def test_plain_text_is_byte_identical(self):
        for text in (
            "就是一句普通的话，没有任何标记。",
            "带 URL https://example.com/a?b=1&c=2 和颜文字 (๑•̀ㅂ•́)و✧",
            "第一段。\n\n第二段，中间空一行。\n\n\n第三段（两个空行）。",
            "缩进的普通行\n    这行也没标记",
            "价格 3.14 元，比例 1:2，邮箱 a_b@c.d",
        ):
            self.assertEqual(clean(text), text, msg=text)

    def test_only_markers_removed_oracle(self):
        """独立的「只删标记」实现作为对照：两者必须一致（样例都不含列表）。"""
        samples = [
            "**粗**和*斜*还有~~删除~~",
            "看 `code` 和 `code2` 这两个",
            "# 标题\n> 引用\n普通文字",
            "**加粗**里带 `代码` 和 __下划线__",
            "（前）**重点**（后）",
        ]
        for text in samples:
            expected = text
            expected = re.sub(r"\*\*(.+?)\*\*", r"\1", expected)
            expected = re.sub(r"__(.+?)__", r"\1", expected)
            expected = re.sub(r"~~(.+?)~~", r"\1", expected)
            expected = re.sub(r"`([^`\n]+)`", r"\1", expected)
            expected = re.sub(r"^#{1,6}\s+", "", expected, flags=re.M)
            expected = re.sub(r"^>\s?", "", expected, flags=re.M)
            self.assertEqual(clean(text), expected, msg=text)


class TestCodeBlocks(unittest.TestCase):
    def test_fence_removed_content_kept(self):
        text = "```python\nprint('hi')\n```"
        self.assertEqual(clean(text), "print('hi')")

    def test_code_block_content_is_not_flattened(self):
        # 代码块里的 "- 这行" 不是列表，`**` 也不是粗体
        text = "```\n- 这行不是列表\n**这不是粗体**\n```"
        self.assertEqual(clean(text), "- 这行不是列表\n**这不是粗体**")

    def test_unclosed_fence_keeps_everything(self):
        text = "```python\nprint('hi')"
        self.assertEqual(clean(text), "print('hi')")

    def test_code_block_keeps_blank_lines(self):
        text = "```\na\n\nb\n```"
        self.assertEqual(clean(text), "a\n\nb")

    def test_inline_code_inside_list_item(self):
        self.assertEqual(clean("1. 用 `tooltip` 看\n2. 再试"), "用 tooltip 看，再试")


# ======================================================================
# 2. 列表
# ======================================================================
class TestLists(unittest.TestCase):
    def test_ordered_variants(self):
        self.assertEqual(clean("1. 甲\n2. 乙"), "甲，乙")
        self.assertEqual(clean("1、甲\n2、乙"), "甲，乙")
        self.assertEqual(clean("1) 甲\n2) 乙"), "甲，乙")
        self.assertEqual(clean("1）甲\n2）乙"), "甲，乙")

    def test_circled_numbers(self):
        self.assertEqual(clean("① 甲\n② 乙\n③ 丙"), "甲，乙，丙")
        self.assertEqual(clean("① 甲 ② 乙 ③ 丙"), "甲，乙，丙")

    def test_unordered_variants(self):
        for marker in ("-", "*", "•", "·"):
            self.assertEqual(clean(f"{marker} 甲\n{marker} 乙"), "甲，乙", msg=marker)

    def test_nested_list(self):
        self.assertEqual(clean("- 甲\n  - 乙\n    - 丙"), "甲，乙，丙")

    def test_existing_punctuation_is_kept(self):
        # 真机要求：不能变成「先做A，再做B。」这种丢标点
        self.assertEqual(clean("1. 先做A。2. 再做B。"), "先做A。再做B。")
        self.assertEqual(clean("1. 先做A。\n2. 再做B。"), "先做A。再做B。")
        self.assertEqual(clean("1. 甲？\n2. 乙！"), "甲？乙！")

    def test_no_punctuation_uses_joiner(self):
        self.assertEqual(clean("1. 甲\n2. 乙"), "甲，乙")
        self.assertEqual(clean("1. 甲。\n2. 乙"), "甲。乙")  # 前一项有标点 → 直接接
        self.assertEqual(clean("1. 甲、\n2. 乙"), "甲、乙")  # 顿号也不叠

    def test_custom_joiner(self):
        config = fg.FormatGuardConfig(enable=True, list_joiner="；")
        self.assertEqual(clean("1. 甲\n2. 乙", config), "甲；乙")

    def test_bare_numbered_list_needs_a_run(self):
        # 真机 8 段案例：没有点号的编号清单（连续 1/2/3/4）
        text = "1 控制变量：只改一个\n2 用 debug tooltip 看\n3 再试一次\n4 最后对比"
        self.assertEqual(
            clean(text), "控制变量：只改一个，用 debug tooltip 看，再试一次，最后对比"
        )
        # 单独一行的小数字是内容，不能动
        self.assertEqual(clean("3 人一起过来的"), "3 人一起过来的")
        self.assertEqual(clean("1 个方法就够了"), "1 个方法就够了")

    def test_single_line_inline_markers(self):
        self.assertEqual(clean("1. 甲 2. 乙 3. 丙"), "甲，乙，丙")

    def test_marker_only_lines_have_nothing_to_keep(self):
        self.assertEqual(clean("1.\n2. 甲"), "甲")

    def test_list_item_with_bold(self):
        self.assertEqual(clean("1. **第一点**：小心\n2. 第二点"), "第一点：小心，第二点")

    def test_list_ends_at_blank_line(self):
        text = "1. 甲\n2. 乙\n\n这是另一段普通文字。"
        self.assertEqual(clean(text), "甲，乙\n\n这是另一段普通文字。")

    def test_continuation_line_joins_current_item(self):
        text = "1. 控制变量：只改一个\n   用 tooltip 看\n2. 再试一次"
        self.assertEqual(clean(text), "控制变量：只改一个用 tooltip 看，再试一次")

    def test_english_and_chinese_mixed(self):
        self.assertEqual(clean("1. First item\n2. 第二项"), "First item，第二项")


class TestTables(unittest.TestCase):
    def test_table_flattened_row_by_row(self):
        text = "| 方案 | 优点 |\n|---|---|\n| A | 快 |\n| B | 稳 |"
        self.assertEqual(clean(text), "方案，优点\nA，快\nB，稳")

    def test_separator_row_dropped(self):
        self.assertNotIn("---", clean("| a | b |\n|:--|--:|\n| 1 | 2 |"))

    def test_single_column_table(self):
        self.assertEqual(clean("| 甲 |\n| 乙 |"), "甲\n乙")

    def test_pipe_in_normal_sentence_untouched(self):
        text = "a | b 不是表格"
        self.assertEqual(clean(text), text)


# ======================================================================
# 3. 统计 / 截断 / 配置
# ======================================================================
class TestStats(unittest.TestCase):
    def test_counts_and_lengths(self):
        text = "**粗体**\n\n1. 甲\n2. 乙"
        cleaned, stats = fg.clean(text, CFG)
        self.assertEqual(stats.bold, 1)
        self.assertEqual(stats.list_items, 2)
        self.assertEqual(stats.chars_before, len(text))
        self.assertEqual(stats.chars_after, len(cleaned))
        self.assertIn("1 处粗体", stats.log_line())
        self.assertIn("2 个列表标记", stats.log_line())
        self.assertIn(f"文本 {len(text)}→{len(cleaned)} 字", stats.log_line())

    def test_no_change_summary(self):
        _, stats = fg.clean("普通文本", CFG)
        self.assertFalse(stats.removed_anything)
        self.assertIn("无需处理", stats.log_line())

    def test_code_block_and_inline_code_counted(self):
        _, stats = fg.clean("`a`\n```\nb\n```", CFG)
        self.assertEqual(stats.inline_code, 1)
        self.assertEqual(stats.code_block, 1)


class TestTruncate(unittest.TestCase):
    def test_disabled_by_default(self):
        text = "很长的句子。" * 50
        config = fg.FormatGuardConfig(enable=True, max_chars=0)
        self.assertEqual(clean(text, config), text)

    def test_cut_at_sentence_end(self):
        text = "第一句。第二句。第三句。"
        config = fg.FormatGuardConfig(enable=True, max_chars=9)
        self.assertEqual(clean(text, config), "第一句。第二句。")

    def test_hard_cut_when_no_sentence_end(self):
        text = "没有标点的一长串文字"
        config = fg.FormatGuardConfig(enable=True, max_chars=4)
        self.assertEqual(clean(text, config), "没有标点")

    def test_stats_flag_truncated(self):
        _, stats = fg.clean("第一句。第二句。", fg.FormatGuardConfig(enable=True, max_chars=5))
        self.assertTrue(stats.truncated)
        self.assertIn("截断", stats.log_line())


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        config = fg.FormatGuardConfig()
        self.assertFalse(config.enable)  # ★默认关闭
        self.assertTrue(config.strip_markdown)
        self.assertTrue(config.flatten_lists)
        self.assertEqual(config.list_joiner, "，")
        self.assertEqual(config.max_chars, 0)

    def test_from_raw_missing_is_safe(self):
        config = fg.FormatGuardConfig.from_raw(None)
        self.assertFalse(config.enable)
        self.assertEqual(config.list_joiner, "，")

    def test_from_raw_broken_values(self):
        config = fg.FormatGuardConfig.from_raw(
            {
                "enable": "x",
                "strip_markdown": "nope",
                "flatten_lists": 0,
                "list_joiner": 123,
                "max_chars": "abc",
            }
        )
        self.assertFalse(config.enable)
        self.assertTrue(config.strip_markdown)  # 认不出的字符串 → 用默认值 True
        self.assertFalse(config.flatten_lists)
        self.assertEqual(config.list_joiner, "，")
        self.assertEqual(config.max_chars, 0)

    def test_from_raw_accepts_string_false(self):
        config = fg.FormatGuardConfig.from_raw({"strip_markdown": "false", "enable": "on"})
        self.assertFalse(config.strip_markdown)
        self.assertTrue(config.enable)

    def test_strip_markdown_off_only_flattens(self):
        text = "1. **甲**\n2. 乙"
        self.assertEqual(clean(text, NO_MARKDOWN), "**甲**，乙")

    def test_flatten_lists_off_only_strips_markdown(self):
        text = "1. **甲**\n2. 乙"
        self.assertEqual(clean(text, NO_LISTS), "1. 甲\n2. 乙")

    def test_empty_and_whitespace(self):
        for text in ("", "   ", "\n\n", None):
            cleaned, stats = fg.clean(text, CFG)
            self.assertEqual(cleaned, "" if text is None else text)
            self.assertFalse(stats.removed_anything)


# ======================================================================
# 4. 段数下降（本功能的真正目的）
# ======================================================================
class TestSegmentsReduction(unittest.TestCase):
    """真机症状：编号清单被切成 7~8 条、跨 34 秒。拍平后应当只剩 2~3 段。"""

    REAL_CASE = (
        "1. 控制变量：一次只改一个\n"
        "2. 用 debug tooltip 看当前值\n"
        "3. 改完再跑一遍对比\n"
        "4. 记得看日志里的 warning\n"
        "5. 不行就回滚\n"
        "6. 回滚也要记一笔\n"
        "7. 最后把结论写下来\n"
        "8. 隔天再看一眼"
    )

    def seg_config(self) -> segmented.SegmentedJitterConfig:
        return segmented.SegmentedJitterConfig(enable=True, split_mode="regex", min_segment_chars=6)

    def test_before_is_many_segments(self):
        segments = segmented.split_text(self.REAL_CASE, self.seg_config())
        self.assertGreaterEqual(len(segments), 7, segments)

    def test_after_is_two_or_three_segments(self):
        cleaned = clean(self.REAL_CASE)
        segments = segmented.split_text(cleaned, self.seg_config())
        self.assertLessEqual(len(segments), 3, segments)
        self.assertGreaterEqual(len(segments), 1)

    def test_all_content_survives_flattening(self):
        cleaned = clean(self.REAL_CASE)
        for token in ("控制变量：一次只改一个", "用 debug tooltip 看当前值", "隔天再看一眼"):
            self.assertIn(token, cleaned)


# ======================================================================
# 5. main.py 接线
# ======================================================================
class WiringBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        LOGGER.records.clear()

    async def asyncTearDown(self) -> None:
        plugin = getattr(self, "plugin", None)
        if plugin is not None:
            await plugin.terminate()

    def build(self, ctx=None, **over) -> "plugin_main.ReplyGate":
        self.ctx = ctx or FakeContext()
        self.plugin = plugin_main.ReplyGate(self.ctx, config=make_config(**over))
        return self.plugin


def guard_conf(**over) -> dict:
    base = {
        "enable": True,
        "strip_markdown": True,
        "flatten_lists": True,
        "list_joiner": "，",
        "max_chars": 0,
    }
    base.update(over)
    return base


class TestWiring(WiringBase):
    async def test_disabled_is_zero_side_effect(self):
        calls: list[str] = []
        real = fg.clean

        def spy(text, config):
            calls.append(text)
            return real(text, config)

        fg.clean = spy  # type: ignore[assignment]
        try:
            plugin = self.build()
            event = FakeEvent(chain=[FakePlain("**粗体**")])
            await plugin.apply_reply_delay(event)
        finally:
            fg.clean = real  # type: ignore[assignment]

        self.assertEqual(calls, [], "enable=false 时连 clean 都不许调用")
        self.assertEqual(event.get_result().chain[0].text, "**粗体**")

    async def test_cleans_chain_even_without_segmented_jitter(self):
        """只开 format_guard（分段关着）也要生效——两者解耦。"""
        plugin = self.build(
            segmented_jitter=jitter_conf(enable=False),
            format_guard=guard_conf(),
        )
        event = FakeEvent(chain=[FakePlain("**群聊版：** 今天聊点啥")])
        await plugin.apply_reply_delay(event)
        self.assertEqual(event.get_result().chain[0].text, "群聊版： 今天聊点啥")
        self.assertEqual(self.ctx.sent, [], "分段关着就不该有我们发的消息")
        self.assertTrue(any("去格式化" in l for l in LOGGER.messages("info")))

    async def test_cleans_and_reduces_segments(self):
        plugin = self.build(
            segmented_jitter=jitter_conf(min_segment_chars=6),
            format_guard=guard_conf(),
        )
        event = FakeEvent(
            chain=[FakePlain("1. 甲甲甲甲\n2. 乙乙乙乙\n3. 丙丙丙丙\n4. 丁丁丁丁")]
        )
        await plugin.apply_reply_delay(event)
        text = event.get_result().chain[0].text
        self.assertNotIn("1.", text)
        self.assertIn("甲甲甲甲", text)
        self.assertIn("丁丁丁丁", text)
        # 只剩第一段在 result 里（其余段交给补发任务，见 test_segmented_jitter）
        self.assertTrue(
            any("去格式化" in l for l in LOGGER.messages("info")), LOGGER.messages()
        )

    async def test_non_text_chain_is_never_touched(self):
        calls: list[str] = []
        real = fg.clean

        def spy(text, config):
            calls.append(text)
            return real(text, config)

        fg.clean = spy  # type: ignore[assignment]
        try:
            plugin = self.build(format_guard=guard_conf())
            event = FakeEvent(chain=[FakePlain("**粗体**"), object()])
            await plugin.apply_reply_delay(event)
        finally:
            fg.clean = real  # type: ignore[assignment]

        self.assertEqual(calls, [], "有非文本段时整条不动")
        self.assertEqual(event.get_result().chain[0].text, "**粗体**")
        self.assertEqual(len(event.get_result().chain), 2)

    async def test_exception_in_clean_falls_back_to_original(self):
        def boom(text, config):
            raise RuntimeError("去格式化炸了")

        plugin = self.build(segmented_jitter=jitter_conf(enable=False), format_guard=guard_conf())
        original = fg.clean
        fg.clean = boom  # type: ignore[assignment]
        try:
            event = FakeEvent(chain=[FakePlain("**粗体** 保留我")])
            await plugin.apply_reply_delay(event)  # 不许抛
        finally:
            fg.clean = original  # type: ignore[assignment]

        self.assertEqual(event.get_result().chain[0].text, "**粗体** 保留我")
        self.assertTrue(any("去格式化" in l for l in LOGGER.messages("warning")))

    async def test_stopped_event_does_not_clean(self):
        calls: list[str] = []
        real = fg.clean

        def spy(text, config):
            calls.append(text)
            return real(text, config)

        fg.clean = spy  # type: ignore[assignment]
        try:
            plugin = self.build(format_guard=guard_conf())
            event = FakeEvent(chain=[FakePlain("**粗体**")], stopped=True)
            await plugin.apply_reply_delay(event)
        finally:
            fg.clean = real  # type: ignore[assignment]

        self.assertEqual(calls, [], "被停掉的事件不该出现「去格式化」")


class TestSchemaAndWhitelist(unittest.TestCase):
    def test_schema_has_format_guard_group(self):
        import json

        schema = json.loads(
            (REPO_ROOT / "life" / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("format_guard", schema)
        items = schema["format_guard"]["items"]
        self.assertEqual(
            set(items),
            {"enable", "strip_markdown", "flatten_lists", "list_joiner", "max_chars"},
        )
        self.assertIs(items["enable"]["default"], False)  # ★默认关闭
        self.assertIs(items["strip_markdown"]["default"], True)
        self.assertIs(items["flatten_lists"]["default"], True)
        self.assertEqual(items["list_joiner"]["default"], "，")
        self.assertEqual(items["max_chars"]["default"], 0)

    def test_settings_whitelist_contains_format_guard(self):
        self.assertIn("format_guard", webapi.SETTINGS_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
