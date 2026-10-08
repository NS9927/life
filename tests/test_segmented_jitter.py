"""分段回复的时间模型（打字抖动）+「正在输入」测试。

三层：

1. **切分**（``core/segmented.split_text``）：按标点切、保留标点、短段合并、
   超 max_segments 并尾、无标点长文本、纯英文/中英混排。
2. **时间数学**（``core/segmented.plan_timing``）：先读再打、抖动比例、
   ``pre_min/pre_max``、段间 ``min/max``、``max_total_seconds`` 压缩与保底、
   长回复更久 + 到顶不再增长。
3. **接线**（``main.py`` 的 ``on_decorating_result``）：零副作用、不重不漏、
   非文本段放行、发送失败不吞回复、跳过 reply_delay、正在输入任务启停与失败降级。
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "life"))

from tests._astrbot_stub import FakePlain, install  # noqa: E402

LOGGER = install()  # 必须在 import main 之前（幂等）

from core import segmented  # noqa: E402
from core import typing_indicator as typing  # noqa: E402
from life import main as plugin_main  # noqa: E402
from life import webapi  # noqa: E402

UMO = "aiocqhttp:FriendMessage:3472782072"
GROUP_UMO = "aiocqhttp:GroupMessage:100"


class FixedRng:
    """确定性 rng：uniform 固定返回一个值（用来把抖动系数钉死）。"""

    def __init__(self, value: float = 1.0) -> None:
        self.value = value

    def random(self) -> float:
        return 0.5

    def uniform(self, low: float, high: float) -> float:
        return self.value


def cfg(**over) -> segmented.SegmentedJitterConfig:
    """纯逻辑测试的默认配置：显式用 ``chars`` 模式（字符级行为最好断言）。

    ``regex`` 模式（现在的出厂默认）在 TestRegexMode / TestNewlineHardBoundary 里单测。
    """
    base = dict(
        enable=True,
        typing_chars_per_second=3.5,
        reading_chars_per_second=12.0,
        pre_min_seconds=0.8,
        pre_max_seconds=15.0,
        min_seconds=0.6,
        max_seconds=3.0,
        jitter=0.0,
        max_segments=4,
        max_total_seconds=30.0,
        split_mode="chars",
        split_chars=segmented.DEFAULT_SPLIT_CHARS,
        min_segment_chars=6,
        count_incoming_chars=True,
    )
    base.update(over)
    return segmented.SegmentedJitterConfig(**base)


def regex_cfg(**over) -> segmented.SegmentedJitterConfig:
    base = dict(
        enable=True,
        jitter=0.0,
        max_segments=4,
        max_total_seconds=30.0,
        split_mode="regex",
        regex=segmented.DEFAULT_REGEX,
        min_segment_chars=6,
    )
    base.update(over)
    return segmented.SegmentedJitterConfig(**base)


# 任务书给的真实样例（A~E）：两种模式都要每行成段
SAMPLES = {
    "A": "想吃啥？\n我随便。\n那吃面吧。",
    "B": "先说一下结论。\n第一点是这样的，要慢慢讲。\n第二点更复杂一些，得展开说。",
    "C": "一。\n\n二。\n三。",
    "D": "这是一条没有任何标点也没有换行的很长很长很长很长很长很长很长很长的句子",
    "E": "Windows 换行\r\n也要能切\r\n还有\r",
}

EXPECTED_LINES = {
    "A": ["想吃啥？", "我随便。", "那吃面吧。"],
    "B": ["先说一下结论。", "第一点是这样的，要慢慢讲。", "第二点更复杂一些，得展开说。"],
    "C": ["一。", "二。", "三。"],
    "D": ["这是一条没有任何标点也没有换行的很长很长很长很长很长很长很长很长的句子"],
    "E": ["Windows 换行", "也要能切", "还有"],
}

# 内置 result_decorate/stage.py:92（默认值）与真机 abconf 里的现值
BUILTIN_DEFAULT_REGEX = r".*?[。？！~…]+|.+$"
REAL_MACHINE_REGEX = r"[^\n]*?[。？！~…（）]+|[^\n]+"


def builtin_split(text: str, pattern: str) -> list[str]:
    """照抄内置 result_decorate/stage.py:226-263 的做法：findall + strip + 丢空段。"""
    found = re.findall(pattern, text, re.DOTALL | re.MULTILINE)
    if not found:
        return [text]
    return [seg for seg in (item.strip() for item in found) if seg]


# ======================================================================
# 1. 切分
# ======================================================================
class TestSplitText(unittest.TestCase):
    def test_splits_after_punctuation_and_keeps_it(self):
        text = "今天天气不错。你吃饭了吗？我还没吃！"
        parts = segmented.split_text(text, cfg(min_segment_chars=0))
        self.assertEqual(parts, ["今天天气不错。", "你吃饭了吗？", "我还没吃！"])
        self.assertEqual("".join(parts), text)

    def test_never_changes_characters(self):
        # 换行符本身不进正文（内置 regex 也不含换行），所以去掉换行后必须逐字相同
        text = "第一句。第二句！第三句？\n还有一行；结束"
        for config in (cfg(min_segment_chars=0, max_segments=2), regex_cfg(max_segments=2)):
            parts = segmented.split_text(text, config)
            self.assertEqual("".join(parts), text.replace("\n", ""), msg=config.split_mode)

    def test_strips_outer_whitespace_only(self):
        parts = segmented.split_text("  你好。世界。  ", cfg(min_segment_chars=0))
        self.assertEqual("".join(parts), "你好。世界。")

    def test_empty_or_whitespace_returns_nothing(self):
        for bad in ["", "   ", "\n\n", None]:
            self.assertEqual(segmented.split_text(bad, cfg()), [], msg=repr(bad))

    def test_short_segments_merge_into_previous(self):
        # 「嗯。」只有 2 字 → 并进前一段
        parts = segmented.split_text("今天过得还行。嗯。", cfg(min_segment_chars=6))
        self.assertEqual(parts, ["今天过得还行。嗯。"])

    def test_first_short_segment_merges_forward(self):
        parts = segmented.split_text("好。今天过得还行，挺充实的。", cfg(min_segment_chars=6))
        self.assertEqual(parts, ["好。今天过得还行，挺充实的。"])

    def test_max_segments_merges_tail(self):
        text = "第一段内容啊。第二段内容啊。第三段内容啊。第四段内容啊。第五段内容啊。"
        parts = segmented.split_text(text, cfg(min_segment_chars=0, max_segments=3))
        self.assertEqual(len(parts), 3)
        self.assertEqual("".join(parts), text)
        self.assertTrue(parts[-1].startswith("第三段内容啊。"))
        self.assertIn("第五段内容啊。", parts[-1])

    def test_no_punctuation_stays_single(self):
        text = "这是一句没有任何标点的超长文本" * 5
        parts = segmented.split_text(text, cfg(min_segment_chars=0))
        self.assertEqual(parts, [text])

    def test_english_and_mixed(self):
        # 默认切分字符里**没有** ASCII 句点（规格给的就是 "。！？!?\n…；;"），
        # 所以英文句号不断句；想断就把 '.' 加进 split_chars
        parts = segmented.split_text("Hello there. 你好呀！OK?", cfg(min_segment_chars=0))
        self.assertEqual(parts, ["Hello there. 你好呀！", "OK?"])
        with_dot = segmented.split_text(
            "Hello there. 你好呀！OK?", cfg(split_chars=".!?。！", min_segment_chars=0)
        )
        self.assertEqual(with_dot, ["Hello there.", " 你好呀！", "OK?"])

    def test_newline_is_a_split_char(self):
        # 换行永远是边界；换行符本身不进正文
        for config in (cfg(min_segment_chars=0), regex_cfg()):
            parts = segmented.split_text("第一行\n第二行\n", config)
            self.assertEqual(parts, ["第一行", "第二行"], msg=config.split_mode)
            self.assertEqual("".join(parts), "第一行第二行")

    def test_custom_split_chars(self):
        parts = segmented.split_text("a|b|c", cfg(split_chars="|", min_segment_chars=0))
        self.assertEqual(parts, ["a|", "b|", "c"])

    def test_no_split_chars_configured_keeps_whole(self):
        parts = segmented.split_text("随便什么文本。", cfg(split_chars="", min_segment_chars=0))
        self.assertEqual(parts, ["随便什么文本。"])


class TestNewlineHardBoundary(unittest.TestCase):
    """真机反馈的核心：**换行必须分段**，短行不许被 min_segment_chars 合并回去。"""

    def test_samples_chars_mode(self):
        config = cfg(min_segment_chars=6, max_segments=4)
        for name, text in SAMPLES.items():
            parts = segmented.split_text(text, config)
            self.assertEqual(parts, EXPECTED_LINES[name], msg=f"样例 {name}（chars 模式）")

    def test_one_and_two_char_lines_survive(self):
        # 每条换行行只有 1~3 个字 → 一行一段，绝不合并
        text = "好\n嗯\n行"
        for config in (cfg(min_segment_chars=6), regex_cfg(min_segment_chars=6)):
            self.assertEqual(segmented.split_text(text, config), ["好", "嗯", "行"],
                             msg=config.split_mode)
        text2 = "想吃啥？\n我随便。\n那吃面吧。"
        for config in (cfg(min_segment_chars=6), regex_cfg(min_segment_chars=6)):
            self.assertEqual(len(segmented.split_text(text2, config)), 3, msg=config.split_mode)

    def test_blank_lines_do_not_produce_empty_segments(self):
        parts = segmented.split_text("一。\n\n\n二。", cfg(min_segment_chars=0))
        self.assertEqual(parts, ["一。", "二。"])
        self.assertEqual(segmented.split_text("\n\n", cfg()), [])

    def test_crlf_and_cr_are_boundaries(self):
        for config in (cfg(min_segment_chars=0), regex_cfg()):
            self.assertEqual(
                segmented.split_text("Windows 换行\r\n也要能切\r\n还有\r", config),
                ["Windows 换行", "也要能切", "还有"],
                msg=config.split_mode,
            )

    def test_literal_backslash_n_config_also_splits(self):
        """面板/写回把配置改成两字符 ``\\n`` 时，也要能按换行切（chars 模式兜底）。"""
        config = cfg(split_chars="。！？!?\\n…；;", min_segment_chars=6)
        self.assertIn(segmented.LITERAL_NEWLINE, config.split_chars)
        self.assertEqual([ord(c) for c in segmented.LITERAL_NEWLINE], [92, 110])  # \ + n
        # 文本里是真换行 → 照切
        self.assertEqual(
            segmented.split_text("想吃啥？\n我随便。\n那吃面吧。", config),
            ["想吃啥？", "我随便。", "那吃面吧。"],
        )
        # 文本里是字面量 \n → 也还原成换行切
        self.assertEqual(
            segmented.split_text("想吃啥？\\n我随便。\\n那吃面吧。", config),
            ["想吃啥？", "我随便。", "那吃面吧。"],
        )

    def test_max_segments_never_merges_lines(self):
        # 6 行、每行 2 个字，max_segments=2 → 行与行绝不合并（换行必切）
        text = "一。\n二。\n三。\n四。\n五。\n六。"
        for config in (cfg(min_segment_chars=0, max_segments=2), regex_cfg(max_segments=2)):
            self.assertEqual(
                segmented.split_text(text, config),
                ["一。", "二。", "三。", "四。", "五。", "六。"],
                msg=config.split_mode,
            )

    def test_max_segments_still_merges_punctuation_within_a_line(self):
        # 单行 5 个标点段、上限 3 → 多出来的并进最后一段（老的软语义仍然保留）
        text = "第一段内容啊。第二段内容啊。第三段内容啊。第四段内容啊。第五段内容啊。"
        for config in (cfg(min_segment_chars=0, max_segments=3), regex_cfg(max_segments=3)):
            parts = segmented.split_text(text, config)
            self.assertEqual(len(parts), 3, msg=config.split_mode)
            self.assertIn("第五段内容啊。", parts[-1])


class TestRegexMode(unittest.TestCase):
    """``split_mode="regex"``（出厂默认）：与内置 segmented_reply 对齐。"""

    def setUp(self) -> None:
        segmented.clear_caches()

    def test_samples_regex_mode(self):
        config = regex_cfg()
        for name, text in SAMPLES.items():
            self.assertEqual(
                segmented.split_text(text, config), EXPECTED_LINES[name], msg=f"样例 {name}"
            )

    def test_default_regex_is_line_based(self):
        # [^\n] 明确排除换行 → 每个换行行独立成段
        self.assertEqual(
            segmented.split_text("想吃啥？\n我随便。\n那吃面吧。", regex_cfg()),
            ["想吃啥？", "我随便。", "那吃面吧。"],
        )

    def test_unpunctuated_long_line_stays_whole(self):
        self.assertEqual(segmented.split_text(SAMPLES["D"], regex_cfg()), [SAMPLES["D"]])

    def test_min_segment_chars_is_ignored(self):
        # 1 个字的段照样独立（正则已经决定怎么切）
        self.assertEqual(segmented.split_text("好\n嗯\n行", regex_cfg(min_segment_chars=99)),
                         ["好", "嗯", "行"])

    def test_matches_builtin_on_real_machine_regex(self):
        """真机同款正则下，我们的输出必须和内置逐段一致。"""
        for text in (SAMPLES["A"], SAMPLES["B"], SAMPLES["C"], SAMPLES["E"]):
            ours = segmented.split_text(text, regex_cfg(regex=REAL_MACHINE_REGEX))
            theirs = builtin_split(text.replace("\r\n", "\n").replace("\r", "\n"), REAL_MACHINE_REGEX)
            self.assertEqual(ours, theirs, msg=text)

    def test_matches_builtin_default_regex_too(self):
        for text in (SAMPLES["A"], SAMPLES["C"]):
            ours = segmented.split_text(text, regex_cfg(regex=BUILTIN_DEFAULT_REGEX))
            theirs = builtin_split(text, BUILTIN_DEFAULT_REGEX)
            self.assertEqual(ours, theirs, msg=text)

    def test_no_match_keeps_whole_text(self):
        # 正则能编译但一段都没匹配到 → 整条不切（内置此时也是原样保留）
        self.assertEqual(segmented.split_text("abc", regex_cfg(regex="QQQ")), ["abc"])

    def test_broken_regex_falls_back_to_chars(self):
        warnings: list[tuple[str, str]] = []
        config = regex_cfg(regex="(", split_chars=segmented.DEFAULT_SPLIT_CHARS)
        parts = segmented.split_text(SAMPLES["A"], config, on_error=lambda p, e: warnings.append((p, str(e))))
        self.assertEqual(parts, EXPECTED_LINES["A"])
        self.assertEqual(len(warnings), 1, "只警告一次")

    def test_broken_regex_warns_only_once_across_calls(self):
        warnings: list[str] = []
        config = regex_cfg(regex="[")
        for _ in range(5):
            segmented.split_text(SAMPLES["A"], config, on_error=lambda p, e: warnings.append(p))
        self.assertEqual(len(warnings), 1)

    def test_clear_caches_allows_warning_again(self):
        warnings: list[str] = []
        config = regex_cfg(regex="(")
        segmented.split_text(SAMPLES["A"], config, on_error=lambda p, e: warnings.append(p))
        segmented.clear_caches()
        segmented.split_text(SAMPLES["A"], config, on_error=lambda p, e: warnings.append(p))
        self.assertEqual(len(warnings), 2)

    def test_on_error_failure_never_propagates(self):
        def boom(_pattern, _exc):
            raise RuntimeError("记日志炸了")

        self.assertEqual(
            segmented.split_text(SAMPLES["A"], regex_cfg(regex="("), on_error=boom),
            EXPECTED_LINES["A"],
        )

    def test_validate_regex_helper(self):
        self.assertTrue(segmented.validate_regex(segmented.DEFAULT_REGEX))
        self.assertFalse(segmented.validate_regex("("))

    def test_capture_groups_are_flattened(self):
        # 用户写了捕获组 → findall 会给元组；我们要能拼回字符串而不是崩
        parts = segmented.split_text("a1 b2", regex_cfg(regex=r"([a-z])(\d)"))
        self.assertEqual(parts, ["a1", "b2"])


# ======================================================================
# 2. 时间数学
# ======================================================================
class TestTimingMath(unittest.TestCase):
    def plan(self, incoming, segments, config=None, rng=None):
        return segmented.plan_timing(incoming, segments, config or cfg(), rng or FixedRng(1.0))

    def test_reading_plus_typing_formula(self):
        # 入站 24 字 / 12 字每秒 = 2s 阅读；回复 7 字 / 3.5 = 2s 打字 → pre_delay 4s
        plan = self.plan(24, ["你好呀，今天啊"])
        self.assertAlmostEqual(plan.reading_seconds, 2.0)
        self.assertAlmostEqual(plan.typing_seconds, 2.0)
        self.assertAlmostEqual(plan.pre_delay, 4.0)
        self.assertEqual(plan.reply_chars, 7)

    def test_pre_delay_clamped_to_bounds(self):
        # 极短：落到 pre_min
        short = self.plan(0, ["嗯"])
        self.assertAlmostEqual(short.pre_delay, 0.8)
        # 极长：落到 pre_max（不会无限长）
        long = self.plan(100000, ["字" * 100000])
        self.assertAlmostEqual(long.pre_delay, 15.0)

    def test_pre_delay_always_within_bounds_with_jitter(self):
        config = cfg(jitter=0.25)
        for seed in range(50):
            plan = self.plan(30, ["一二三四五六七八九十。"], config, random.Random(seed))
            self.assertGreaterEqual(plan.pre_delay, config.pre_min_seconds)
            self.assertLessEqual(plan.pre_delay, config.pre_max_seconds)

    def test_segment_delays_within_bounds_and_count(self):
        config = cfg(jitter=0.25)
        parts = ["一二三四五六七八九十。", "短。", "再一段内容啊。"]
        for seed in range(50):
            plan = self.plan(20, parts, config, random.Random(seed))
            self.assertEqual(len(plan.segment_delays), len(parts) - 1)  # N 段 → N-1 个间隔
            for delay in plan.segment_delays:
                self.assertGreaterEqual(delay, config.min_seconds)
                self.assertLessEqual(delay, config.max_seconds)

    def test_segment_delay_uses_previous_segment_length(self):
        config = cfg(min_seconds=0.0, max_seconds=100.0, jitter=0.0, pre_min_seconds=0.0, pre_max_seconds=0.0)
        short_then_long = self.plan(0, ["短。", "字" * 35 + "。"], config)
        long_then_short = self.plan(0, ["字" * 35 + "。", "短。"], config)
        self.assertAlmostEqual(short_then_long.segment_delays[0], 2 / 3.5)
        self.assertAlmostEqual(long_then_short.segment_delays[0], 36 / 3.5)

    def test_jitter_ratio_actually_varies(self):
        config = cfg(jitter=0.5, pre_min_seconds=0.0, pre_max_seconds=100.0)
        values = {
            round(self.plan(0, ["一二三四五六七八九十"], config, random.Random(seed)).pre_delay, 4)
            for seed in range(20)
        }
        self.assertGreater(len(values), 5)

    def test_jitter_zero_is_deterministic(self):
        config = cfg(jitter=0.0)
        first = self.plan(12, ["你好呀。"], config, random.Random(1))
        second = self.plan(12, ["你好呀。"], config, random.Random(999))
        self.assertEqual(first.pre_delay, second.pre_delay)

    def test_total_cap_scales_down(self):
        # 4 段、每段很长 → 不压缩会远超 30s（pre 15 + 3×3）
        config = cfg(max_total_seconds=6.0)
        parts = ["字" * 200 + "。"] * 4
        plan = self.plan(0, parts, config)
        self.assertTrue(plan.scaled)
        self.assertLessEqual(plan.total, 6.0 + 1e-9)

    def test_cap_keeps_every_item_above_floor(self):
        config = cfg(max_total_seconds=1.0)
        parts = ["字" * 200 + "。"] * 4
        plan = self.plan(0, parts, config)
        self.assertGreaterEqual(plan.pre_delay, config.pre_min_seconds * segmented.FLOOR_RATIO)
        for delay in plan.segment_delays:
            self.assertGreaterEqual(delay, config.min_seconds * segmented.FLOOR_RATIO)

    def test_cap_never_produces_zero_delays(self):
        # 极端：上限 0.01s，压缩后仍然每一段都有保底，不会「连发」
        config = cfg(max_total_seconds=0.01)
        plan = self.plan(0, ["字" * 500 + "。"] * 4, config)
        self.assertGreater(plan.pre_delay, 0.0)
        for delay in plan.segment_delays:
            self.assertGreater(delay, 0.0)

    def test_zero_cap_means_no_limit(self):
        config = cfg(max_total_seconds=0.0)
        plan = self.plan(0, ["字" * 200 + "。"] * 4, config)
        self.assertFalse(plan.scaled)

    def test_long_reply_takes_longer_until_cap(self):
        """长回复打字更久；到 pre_max 到顶后不再增长（避免让人等一分钟）。"""
        config = cfg(pre_max_seconds=15.0)
        short = self.plan(0, ["一二三四五"], config)  # 5 字 → 1.43s
        medium = self.plan(0, ["字" * 30], config)  # 30 字 → 8.57s
        at_cap = self.plan(0, ["字" * 53], config)  # 53 字 → 15.1s → 截到 15
        beyond = self.plan(0, ["字" * 300], config)  # 300 字 → 仍 15
        self.assertLess(short.pre_delay, medium.pre_delay)
        self.assertLess(medium.pre_delay, at_cap.pre_delay)
        self.assertAlmostEqual(at_cap.pre_delay, 15.0)
        self.assertAlmostEqual(beyond.pre_delay, 15.0)

    def test_incoming_count_can_be_disabled(self):
        on = self.plan(1200, ["你好呀。"], cfg(count_incoming_chars=True))
        off = self.plan(1200, ["你好呀。"], cfg(count_incoming_chars=False))
        self.assertGreater(on.reading_seconds, 0)
        self.assertEqual(off.reading_seconds, 0.0)
        self.assertLess(off.pre_delay, on.pre_delay)

    def test_missing_incoming_is_zero_and_never_negative(self):
        plan = self.plan(-5, ["你好呀。"])
        self.assertEqual(plan.incoming_chars, 0)
        self.assertEqual(plan.reading_seconds, 0.0)

    def test_log_line_contains_everything_needed(self):
        plan = self.plan(12, ["你好呀。", "再见。"], cfg())
        line = plan.log_line()
        for token in ("入站 12 字", "阅读", "打字", "pre_delay", "段间隔"):
            self.assertIn(token, line)


class TestSegmentedConfig(unittest.TestCase):
    def test_defaults_match_schema(self):
        config = segmented.SegmentedJitterConfig()
        self.assertFalse(config.enable)  # ★默认关闭
        self.assertEqual(config.typing_chars_per_second, 3.5)
        self.assertEqual(config.reading_chars_per_second, 12.0)
        self.assertEqual(config.pre_max_seconds, 15.0)  # 长回复：上限已放开
        self.assertEqual(config.max_total_seconds, 30.0)
        self.assertEqual(config.max_segments, 4)

    def test_from_raw_missing_is_safe(self):
        config = segmented.SegmentedJitterConfig.from_raw(None)
        self.assertFalse(config.enable)
        self.assertEqual(config.pre_max_seconds, 15.0)
        self.assertEqual(config.max_total_seconds, 30.0)

    def test_broken_values_fall_back(self):
        config = segmented.SegmentedJitterConfig.from_raw(
            {
                "enable": "x",
                "typing_chars_per_second": "abc",
                "reading_chars_per_second": None,
                "pre_min_seconds": [],
                "jitter": "NaN",
                "max_segments": "y",
                "min_segment_chars": -3,
                "count_incoming_chars": "nope",
                "split_chars": 12345,
            }
        )
        self.assertFalse(config.enable)
        self.assertEqual(config.typing_chars_per_second, 3.5)
        self.assertEqual(config.reading_chars_per_second, 12.0)
        self.assertEqual(config.pre_min_seconds, 0.8)
        self.assertEqual(config.jitter, 0.25)
        self.assertEqual(config.max_segments, 4)
        self.assertEqual(config.min_segment_chars, 0)
        self.assertTrue(config.count_incoming_chars)
        self.assertEqual(config.split_chars, segmented.DEFAULT_SPLIT_CHARS)

    def test_reversed_bounds_are_normalised(self):
        config = segmented.SegmentedJitterConfig.from_raw(
            {"pre_min_seconds": 9, "pre_max_seconds": 2, "min_seconds": 5, "max_seconds": 1}
        )
        self.assertEqual((config.pre_min_seconds, config.pre_max_seconds), (2, 9))
        self.assertEqual((config.min_seconds, config.max_seconds), (1, 5))

    def test_jitter_clamped_and_speed_positive(self):
        config = segmented.SegmentedJitterConfig.from_raw(
            {"jitter": 5, "typing_chars_per_second": 0, "reading_chars_per_second": -1}
        )
        self.assertEqual(config.jitter, 1.0)
        self.assertGreater(config.typing_chars_per_second, 0)
        self.assertGreater(config.reading_chars_per_second, 0)


# ======================================================================
# 3. 「正在输入」内核
# ======================================================================
class TestTypingConfig(unittest.TestCase):
    def test_defaults(self):
        config = typing.TypingIndicatorConfig()
        self.assertFalse(config.enable)  # ★默认关闭
        self.assertEqual(config.refresh_seconds, 4.0)
        self.assertTrue(config.private)
        self.assertFalse(config.group)  # NapCat 群聊不支持

    def test_from_raw_broken_is_safe(self):
        config = typing.TypingIndicatorConfig.from_raw(
            {"enable": "off", "refresh_seconds": "abc", "private": "yes", "group": "no"}
        )
        self.assertFalse(config.enable)
        self.assertEqual(config.refresh_seconds, 4.0)
        self.assertTrue(config.private)
        self.assertFalse(config.group)

    def test_refresh_has_lower_bound(self):
        config = typing.TypingIndicatorConfig.from_raw({"refresh_seconds": 0})
        self.assertEqual(config.refresh_seconds, typing.MIN_REFRESH_SECONDS)

    def test_allows_matrix(self):
        off = typing.TypingIndicatorConfig(enable=False, private=True, group=True)
        self.assertFalse(off.allows(True))
        self.assertFalse(off.allows(False))
        on = typing.TypingIndicatorConfig(enable=True, private=True, group=False)
        self.assertTrue(on.allows(True))
        self.assertFalse(on.allows(False))
        group_only = typing.TypingIndicatorConfig(enable=True, private=False, group=True)
        self.assertTrue(group_only.allows(False))
        self.assertFalse(group_only.allows(True))

    def test_describe(self):
        line = typing.TypingIndicatorConfig(enable=True).describe()
        self.assertIn("私聊=on", line)
        self.assertIn("群聊=off", line)
        self.assertIn("续期间隔=4.0s", line)


class TestTypingIndicator(unittest.IsolatedAsyncioTestCase):
    def make(self, sender, **over):
        config = typing.TypingIndicatorConfig(
            enable=over.pop("enable", True),
            refresh_seconds=over.pop("refresh_seconds", 0.02),
            private=over.pop("private", True),
            group=over.pop("group", False),
        )
        self.calls: list[typing.TypingTarget] = []

        async def default_sender(target):
            self.calls.append(target)

        indicator = typing.TypingIndicator(config, logger=LOGGER)
        self.sender = sender or default_sender
        return indicator

    def target(self, is_private=True, umo=UMO, user_id="3472782072"):
        return typing.TypingTarget(umo=umo, is_private=is_private, user_id=user_id)

    async def test_disabled_has_zero_side_effects(self):
        async def sender(target):
            self.calls.append(target)

        self.calls = []
        indicator = typing.TypingIndicator(
            typing.TypingIndicatorConfig(enable=False), logger=LOGGER
        )
        async with indicator.indicator(self.target(), sender):
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.05)
        self.assertEqual(self.calls, [])

    async def test_refreshes_during_wait_and_stops_after(self):
        indicator = self.make(None)
        async with indicator.indicator(self.target(), self.sender):
            await asyncio.sleep(0.1)
        during = len(self.calls)
        self.assertGreaterEqual(during, 2, "等待期间应该续期多次")
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.calls), during, "退出后不许再调用")
        self.assertEqual(self.calls[0].user_id, "3472782072")

    async def test_private_off_group_on(self):
        indicator = self.make(None, private=False, group=True)
        async with indicator.indicator(self.target(is_private=True), self.sender):
            await asyncio.sleep(0.05)
        self.assertEqual(self.calls, [], "私聊关了就一次都不发")
        async with indicator.indicator(self.target(is_private=False), self.sender):
            await asyncio.sleep(0.05)
        self.assertGreaterEqual(len(self.calls), 1, "群聊开了才发")

    async def test_failure_is_swallowed_and_marked_unsupported(self):
        attempts = []

        async def bad_sender(target):
            attempts.append(target)
            raise RuntimeError("不支持的 API")

        indicator = typing.TypingIndicator(
            typing.TypingIndicatorConfig(enable=True, refresh_seconds=0.01), logger=LOGGER
        )
        LOGGER.records.clear()
        # 不抛异常 → 回复流程不受影响
        async with indicator.indicator(self.target(), bad_sender):
            await asyncio.sleep(0.05)
        self.assertEqual(len(attempts), 1, "失败后立刻停手，不再重试")
        self.assertIn(UMO, indicator.unsupported)
        debug_lines = [l for l in LOGGER.messages("debug") if "正在输入" in l]
        self.assertEqual(len(debug_lines), 1, "只记一次 debug")

        # 第二次（同一会话）连调用都不该有
        async with indicator.indicator(self.target(), bad_sender):
            await asyncio.sleep(0.03)
        self.assertEqual(len(attempts), 1)

    async def test_timeout_counts_as_unsupported(self):
        async def slow_sender(target):
            await asyncio.sleep(5)

        # timeout 有 0.1s 下限（见 TypingIndicator.__init__），所以等 0.4s 让它真的超时
        indicator = typing.TypingIndicator(
            typing.TypingIndicatorConfig(enable=True, refresh_seconds=0.01),
            logger=LOGGER,
            timeout=0.1,
        )
        async with indicator.indicator(self.target(), slow_sender):
            await asyncio.sleep(0.4)
        self.assertIn(UMO, indicator.unsupported)

    async def test_reset_unsupported_retries(self):
        async def bad_sender(target):
            raise RuntimeError("x")

        indicator = typing.TypingIndicator(
            typing.TypingIndicatorConfig(enable=True, refresh_seconds=0.01), logger=LOGGER
        )
        async with indicator.indicator(self.target(), bad_sender):
            await asyncio.sleep(0.03)
        indicator.reset_unsupported()
        async with indicator.indicator(self.target(), bad_sender):
            await asyncio.sleep(0.03)
        self.assertIn(UMO, indicator.unsupported)

    async def test_task_is_not_leaked_on_exception(self):
        indicator = self.make(None)
        before = len(asyncio.all_tasks())
        with self.assertRaises(ValueError):
            async with indicator.indicator(self.target(), self.sender):
                await asyncio.sleep(0.02)
                raise ValueError("正文里炸了")
        await asyncio.sleep(0.05)
        self.assertEqual(len(asyncio.all_tasks()), before, "退出时必须把续期任务收掉")

    async def test_outer_cancellation_propagates_and_stops_refresh(self):
        indicator = self.make(None, refresh_seconds=0.01)

        async def body():
            async with indicator.indicator(self.target(), self.sender):
                await asyncio.sleep(10)

        task = asyncio.create_task(body())
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(len(self.calls), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        count = len(self.calls)
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.calls), count, "被取消后不许再续期")


# ======================================================================
# 4. main.py 接线
# ======================================================================
class FakeImage:
    """非文本段的替身。"""

    def __init__(self, url: str = "http://x/y.png") -> None:
        self.url = url


class FakeMsgObj:
    def __init__(self, message=None, message_str="", message_id="m1") -> None:
        self.message = message if message is not None else []
        self.message_str = message_str
        self.message_id = message_id
        self.timestamp = 0.0
        self.raw_message = None


class FakeResult:
    def __init__(self, chain=None) -> None:
        self.chain = list(chain) if chain is not None else []


class FakeEvent:
    def __init__(
        self,
        *,
        umo=UMO,
        private=True,
        chain=None,
        incoming=None,
        incoming_str="",
        bot=None,
        stopped=False,
        result=True,
    ) -> None:
        self.unified_msg_origin = umo
        self._private = private
        self._sender = "3472782072"
        self._stopped = stopped
        self._result = FakeResult(chain if chain is not None else [FakePlain("嗯。")]) if result else None
        self.message_obj = FakeMsgObj(message=incoming, message_str=incoming_str)
        self.bot = bot

    def get_sender_id(self):
        return self._sender

    def get_self_id(self):
        return "2920553952"

    def is_private_chat(self):
        return self._private

    def get_sender_name(self):
        return "主人"

    def get_message_str(self):
        return self.message_obj.message_str

    def is_stopped(self):
        return self._stopped

    def get_result(self):
        return self._result

    def stop_event(self):
        self._stopped = True


class FakeBot:
    def __init__(self, *, exc=None) -> None:
        self.calls: list[tuple] = []
        self.exc = exc

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.exc is not None:
            raise self.exc
        return {"status": "ok", "retcode": 0, "data": {"result": 0}}


class FakeContext:
    def __init__(self, *, fail_send_at=None) -> None:
        self.sent: list[tuple[str, object]] = []
        self.fail_send_at = fail_send_at

    def register_web_api(self, *a, **k) -> None:
        pass

    def get_config(self, umo=None) -> dict:
        return {}

    async def send_message(self, session, message_chain) -> bool:
        if self.fail_send_at is not None and len(self.sent) == self.fail_send_at:
            raise RuntimeError("发送挂了")
        self.sent.append((session, message_chain))
        return True


def make_config(**over) -> dict:
    base = {
        "enable": True,
        "time_weights": "0-24=100",
        "base_probability": {"chime": 0.0, "proactive": 0.3, "addressed": 1.0},
        "consecutive_drop_limit": 0,
        "loop_breaker": {"enable": False},
        "session_cooldown": {"enable": False},
        "sleep_queue": {"enable": True, "flush_at_hour": 8, "only_addressed": True},
        "reply_delay": {"enable": False},
        "addressed_reply": {"immediate_threshold": 0.0, "batch_window_seconds": 0.05},
        "silence_list": [],
        "session_whitelist": [],
        "verbose_log": True,
    }
    base.update(over)
    return base


def jitter_conf(**over) -> dict:
    base = {
        "enable": True,
        "typing_chars_per_second": 3.5,
        "reading_chars_per_second": 12.0,
        "pre_min_seconds": 0.0,
        "pre_max_seconds": 0.0,
        "min_seconds": 0.0,
        "max_seconds": 0.0,
        "jitter": 0.0,
        "max_segments": 4,
        "max_total_seconds": 30.0,
        "split_chars": "。！？!?\n…；;",
        "min_segment_chars": 0,
        "count_incoming_chars": True,
    }
    base.update(over)
    return base


def typing_conf(**over) -> dict:
    base = {"enable": True, "refresh_seconds": 4.0, "private": True, "group": False}
    base.update(over)
    return base


@contextlib.contextmanager
def fast_sleep(recorder):
    """把 asyncio.sleep 换成「记账 + 让出 5ms」，避免测试真的等十几秒。"""
    real = asyncio.sleep

    async def fake(seconds, *args, **kwargs):
        recorder.append(seconds)
        await real(0.005)

    with patch("asyncio.sleep", fake):
        yield recorder


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


TEXT = "今天天气不错。你吃饭了吗？我还没吃！"


class TestWiringZeroSideEffects(WiringBase):
    async def test_disabled_does_not_touch_anything(self):
        calls: list[str] = []
        real_split = segmented.split_text

        def spy(text, config):
            calls.append(text)
            return real_split(text, config)

        segmented.split_text = spy  # type: ignore[assignment]
        try:
            plugin = self.build()
            event = FakeEvent(chain=[FakePlain(TEXT)])
            await plugin.apply_reply_delay(event)
        finally:
            segmented.split_text = real_split  # type: ignore[assignment]

        self.assertEqual(calls, [], "enable=false 时不许切分")
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(event.get_result().chain[0].text, TEXT, "结果必须原样")
        self.assertEqual(plugin._typing.unsupported, frozenset())

    async def test_enabled_but_non_text_chain_is_left_alone(self):
        plugin = self.build(segmented_jitter=jitter_conf())
        event = FakeEvent(chain=[FakePlain(TEXT), FakeImage()])
        await plugin.apply_reply_delay(event)
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(len(event.get_result().chain), 2, "非文本段整条不切")
        self.assertEqual(event.get_result().chain[0].text, TEXT)

    async def test_enabled_but_single_segment_is_left_alone(self):
        plugin = self.build(segmented_jitter=jitter_conf())
        event = FakeEvent(chain=[FakePlain("嗯。")])
        await plugin.apply_reply_delay(event)
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(event.get_result().chain[0].text, "嗯。")
        self.assertEqual(plugin._gate.session(UMO).reply_times.__len__(), 1)  # 冷却照记


class TestWiringSplitAndSend(WiringBase):
    async def test_first_segments_sent_last_one_stays_in_result(self):
        plugin = self.build(
            segmented_jitter=jitter_conf(
                min_segment_chars=0,
                pre_min_seconds=0.4,
                pre_max_seconds=0.4,
                min_seconds=0.5,
                max_seconds=0.5,
            )
        )
        event = FakeEvent(chain=[FakePlain(TEXT)])
        with fast_sleep([]) as sleeps:
            await plugin.apply_reply_delay(event)

        sent_texts = [chain.chain[0].text for _, chain in self.ctx.sent]
        self.assertEqual(sent_texts, ["今天天气不错。", "你吃饭了吗？"])
        self.assertEqual(event.get_result().chain[0].text, "我还没吃！", "最后一段留给流水线")
        # 3 段 → 1 个 pre_delay + 2 个段间隔
        self.assertEqual(len(sleeps), 3)
        self.assertAlmostEqual(sleeps[0], 0.4)
        self.assertAlmostEqual(sleeps[1], 0.5)
        self.assertAlmostEqual(sleeps[2], 0.5)
        # 全部内容恰好出现一次（前两段发出 + 最后一段留 result）
        self.assertEqual("".join(sent_texts) + event.get_result().chain[0].text, TEXT)

    async def test_segment_gaps_slept_after_each_sent_segment(self):
        plugin = self.build(
            segmented_jitter=jitter_conf(
                min_segment_chars=0,
                typing_chars_per_second=1.0,
                reading_chars_per_second=1.0,
                pre_min_seconds=0.0,
                pre_max_seconds=100.0,
                min_seconds=0.0,
                max_seconds=100.0,
                max_total_seconds=0.0,  # 关掉总上限，先验原始模型（上限单测另见 test_total_cap_scales_down）
            )
        )
        event = FakeEvent(chain=[FakePlain(TEXT)], incoming=[FakePlain("在吗")])
        with fast_sleep([]) as sleeps:
            await plugin.apply_reply_delay(event)
        # 入站 2 字 /1 = 2s 阅读；回复 18 字 /1 = 18s 打字 → pre_delay 20s
        # 段间 = 上一段字数 /1：7 字 → 7s；6 字 → 6s
        self.assertEqual(len(sleeps), 3)
        self.assertAlmostEqual(sleeps[0], 20.0)
        self.assertAlmostEqual(sleeps[1], 7.0)
        self.assertAlmostEqual(sleeps[2], 6.0)
        self.assertEqual(len(self.plugin._gate.session(UMO).reply_times), 1)

    async def test_total_cap_applies_in_wiring(self):
        """长回复 + 多段时，max_total_seconds 必须真的把总耗时压下来。"""
        plugin = self.build(
            segmented_jitter=jitter_conf(
                min_segment_chars=0,
                typing_chars_per_second=1.0,
                reading_chars_per_second=1.0,
                pre_min_seconds=0.0,
                pre_max_seconds=100.0,
                min_seconds=0.0,
                max_seconds=100.0,
                max_total_seconds=10.0,
            )
        )
        event = FakeEvent(chain=[FakePlain(TEXT)], incoming=[FakePlain("在吗")])
        with fast_sleep([]) as sleeps:
            await plugin.apply_reply_delay(event)
        self.assertLessEqual(sum(sleeps), 10.0 + 1e-9)
        self.assertTrue(any("已按总上限压缩" in l for l in LOGGER.messages() if "打字模型" in l))

    async def test_send_failure_keeps_last_segment_and_never_resends_full_text(self):
        ctx = FakeContext(fail_send_at=1)  # 第二段发送时炸
        plugin = self.build(ctx, segmented_jitter=jitter_conf(min_segment_chars=0))
        event = FakeEvent(chain=[FakePlain(TEXT)])
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)

        self.assertEqual(len(ctx.sent), 1, "第一段发出去了")
        self.assertEqual(event.get_result().chain[0].text, "我还没吃！")
        self.assertTrue(any("分段发送失败" in l for l in LOGGER.messages("exception")))
        # 绝不能出现「完整正文又被发一遍」
        leftover = event.get_result().chain[0].text
        self.assertNotEqual(leftover, TEXT)

    async def test_typing_model_skips_flat_reply_delay(self):
        plugin = self.build(
            segmented_jitter=jitter_conf(min_segment_chars=0),
            reply_delay={"enable": True, "min_seconds": 1, "max_seconds": 2},
        )
        event = FakeEvent(chain=[FakePlain(TEXT)])
        with patch.object(plugin_main, "reply_delay_seconds") as spy:
            with fast_sleep([]):
                await plugin.apply_reply_delay(event)
        spy.assert_not_called()
        self.assertTrue(any("跳过 reply_delay" in l for l in LOGGER.messages()))

    async def test_flat_reply_delay_still_used_when_model_cannot_apply(self):
        plugin = self.build(
            segmented_jitter=jitter_conf(),
            reply_delay={"enable": True, "min_seconds": 1, "max_seconds": 1},
        )
        event = FakeEvent(chain=[FakePlain(TEXT), FakeImage()])
        with patch.object(plugin_main, "reply_delay_seconds", return_value=0.0) as spy:
            with fast_sleep([]):
                await plugin.apply_reply_delay(event)
        spy.assert_called_once()

    async def test_incoming_chars_paths(self):
        plugin = self.build(segmented_jitter=jitter_conf())
        # 1) AstrBotMessage.message 里的 Plain 段（非 Plain 不算）
        event = FakeEvent(incoming=[FakePlain("在吗"), FakeImage(), FakePlain("今天好累")])
        self.assertEqual(plugin._incoming_chars(event), 6)
        # 2) 没有链 → 用 message_str
        event2 = FakeEvent(incoming=[], incoming_str="  今天好累  ")
        self.assertEqual(plugin._incoming_chars(event2), 4)
        # 3) 都拿不到 → 0（不猜、不报错）
        event3 = FakeEvent()
        event3.message_obj = None
        self.assertEqual(plugin._incoming_chars(event3), 0)

    async def test_incoming_count_disabled_reads_nothing(self):
        base = dict(
            min_segment_chars=0,
            pre_min_seconds=0.0,
            pre_max_seconds=30.0,
            min_seconds=0.0,
            max_seconds=0.0,
        )
        plugin = self.build(segmented_jitter=jitter_conf(count_incoming_chars=True, **base))
        event = FakeEvent(chain=[FakePlain(TEXT)], incoming=[FakePlain("在吗")])
        with fast_sleep([]) as with_reading:
            await plugin.apply_reply_delay(event)

        plugin2 = self.build(segmented_jitter=jitter_conf(count_incoming_chars=False, **base))
        event2 = FakeEvent(chain=[FakePlain(TEXT)], incoming=[FakePlain("在吗")])
        with fast_sleep([]) as without_reading:
            await plugin2.apply_reply_delay(event2)

        # 关掉阅读后正好少掉「2 字 / 12 字每秒」那一段
        self.assertAlmostEqual(with_reading[0] - without_reading[0], 2 / 12.0)

    async def test_log_line_has_typing_numbers(self):
        plugin = self.build(segmented_jitter=jitter_conf(min_segment_chars=0))
        event = FakeEvent(chain=[FakePlain(TEXT)], incoming=[FakePlain("在吗")])
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)
        line = next(l for l in LOGGER.messages() if "打字模型" in l)
        for token in ("入站 2 字", "阅读", "打字", "pre_delay", "段间隔"):
            self.assertIn(token, line)


class TestWiringTypingIndicator(WiringBase):
    async def test_disabled_never_calls_bot(self):
        bot = FakeBot()
        plugin = self.build(segmented_jitter=jitter_conf(min_segment_chars=0))
        event = FakeEvent(chain=[FakePlain(TEXT)], bot=bot)
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)
        self.assertEqual(bot.calls, [])

    async def test_enabled_calls_set_input_status_during_wait(self):
        bot = FakeBot()
        plugin = self.build(
            segmented_jitter=jitter_conf(
                min_segment_chars=0, pre_min_seconds=5.0, pre_max_seconds=5.0
            ),
            typing_indicator=typing_conf(refresh_seconds=1.0),
        )
        event = FakeEvent(chain=[FakePlain(TEXT)], bot=bot)
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)
        self.assertGreaterEqual(len(bot.calls), 1)
        action, params = bot.calls[0]
        self.assertEqual(action, "set_input_status")
        self.assertEqual(params["user_id"], "3472782072")
        self.assertEqual(params["event_type"], 1)
        self.assertTrue(
            any("正在输入：私聊=on" in l for l in LOGGER.messages("info")),
            "开启时要有配置日志",
        )

    async def test_group_off_by_default_and_private_flag_respected(self):
        bot = FakeBot()
        plugin = self.build(
            segmented_jitter=jitter_conf(
                min_segment_chars=0, pre_min_seconds=5.0, pre_max_seconds=5.0
            ),
            typing_indicator=typing_conf(),
        )
        event = FakeEvent(chain=[FakePlain(TEXT)], bot=bot, private=False, umo=GROUP_UMO)
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)
        self.assertEqual(bot.calls, [], "群聊默认不发（NapCat 不支持）")

    async def test_group_on_when_explicitly_enabled(self):
        bot = FakeBot()
        plugin = self.build(
            segmented_jitter=jitter_conf(
                min_segment_chars=0, pre_min_seconds=5.0, pre_max_seconds=5.0
            ),
            typing_indicator=typing_conf(group=True),
        )
        event = FakeEvent(chain=[FakePlain(TEXT)], bot=bot, private=False, umo=GROUP_UMO)
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)
        self.assertGreaterEqual(len(bot.calls), 1)

    async def test_zero_wait_does_not_start_indicator(self):
        bot = FakeBot()
        plugin = self.build(
            segmented_jitter=jitter_conf(min_segment_chars=0),
            typing_indicator=typing_conf(),
        )
        event = FakeEvent(chain=[FakePlain(TEXT)], bot=bot)
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)
        self.assertEqual(bot.calls, [], "没有等待就不该出现「正在输入」")

    async def test_bot_failure_never_breaks_the_reply(self):
        bot = FakeBot(exc=RuntimeError("不支持的 API"))
        plugin = self.build(
            segmented_jitter=jitter_conf(
                min_segment_chars=0, pre_min_seconds=5.0, pre_max_seconds=5.0
            ),
            typing_indicator=typing_conf(refresh_seconds=1.0, enable=True),
        )
        event = FakeEvent(chain=[FakePlain(TEXT)], bot=bot)
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)  # 不许抛

        self.assertEqual(event.get_result().chain[0].text, "我还没吃！")
        self.assertIn(UMO, plugin._typing.unsupported)
        self.assertEqual(len([l for l in LOGGER.messages("debug") if "正在输入" in l]), 1)

        # 第二条回复：同一会话不再调用 bot
        calls_before = len(bot.calls)
        event2 = FakeEvent(chain=[FakePlain(TEXT)], bot=bot)
        with fast_sleep([]):
            await plugin.apply_reply_delay(event2)
        self.assertEqual(len(bot.calls), calls_before, "已知不支持就不该再试")

    async def test_no_bot_object_is_treated_as_unsupported(self):
        plugin = self.build(
            segmented_jitter=jitter_conf(
                min_segment_chars=0, pre_min_seconds=5.0, pre_max_seconds=5.0
            ),
            typing_indicator=typing_conf(),
        )
        event = FakeEvent(chain=[FakePlain(TEXT)])  # bot=None
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)
        self.assertIn(UMO, plugin._typing.unsupported)
        self.assertEqual(event.get_result().chain[0].text, "我还没吃！")

    async def test_indicator_stops_when_hook_returns(self):
        bot = FakeBot()
        plugin = self.build(
            segmented_jitter=jitter_conf(
                min_segment_chars=0, pre_min_seconds=5.0, pre_max_seconds=5.0
            ),
            typing_indicator=typing_conf(refresh_seconds=1.0),
        )
        event = FakeEvent(chain=[FakePlain(TEXT)], bot=bot)
        with fast_sleep([]):
            await plugin.apply_reply_delay(event)
        count = len(bot.calls)
        await asyncio.sleep(0.05)
        self.assertEqual(len(bot.calls), count, "钩子返回后不许再续期")


class TestSchemaAndWhitelist(unittest.TestCase):
    def test_schema_has_both_new_groups(self):
        import json

        schema = json.loads(
            (REPO_ROOT / "life" / "_conf_schema.json").read_text(encoding="utf-8")
        )
        jitter = schema["segmented_jitter"]["items"]
        self.assertEqual(
            set(jitter),
            {
                "enable", "typing_chars_per_second", "reading_chars_per_second",
                "pre_min_seconds", "pre_max_seconds", "min_seconds", "max_seconds",
                "jitter", "max_segments", "max_total_seconds",
                "split_mode", "regex", "split_chars",
                "min_segment_chars", "count_incoming_chars",
            },
        )
        self.assertIs(jitter["enable"]["default"], False)
        self.assertEqual(jitter["pre_max_seconds"]["default"], 15.0)
        self.assertEqual(jitter["max_total_seconds"]["default"], 30.0)
        self.assertEqual(jitter["split_mode"]["default"], "regex")
        self.assertEqual(jitter["regex"]["default"], segmented.DEFAULT_REGEX)
        self.assertIn("[^\\n]", jitter["regex"]["default"])  # 行边界靠 [^\n]
        self.assertIn("\n", jitter["split_chars"]["default"])  # 真换行，不是字面量
        self.assertNotIn("\\n", jitter["split_chars"]["default"])

        typing_items = schema["typing_indicator"]["items"]
        self.assertEqual(
            set(typing_items), {"enable", "refresh_seconds", "private", "group"}
        )
        self.assertIs(typing_items["enable"]["default"], False)
        self.assertIs(typing_items["private"]["default"], True)
        self.assertIs(typing_items["group"]["default"], False)  # NapCat 群聊不支持

    def test_settings_whitelist_contains_new_groups(self):
        self.assertIn("segmented_jitter", webapi.SETTINGS_KEYS)
        self.assertIn("typing_indicator", webapi.SETTINGS_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
