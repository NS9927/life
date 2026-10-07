"""主动开口（PROACTIVE）测试：纯逻辑 + main.py 接线。

分两层，和 codebase 其余测试一致：

1. **纯逻辑**（``life/core/proactive.py``）：不装 astrbot 桩也能跑，时间/rng 全注入。
2. **接线**（``main.py``）：用 ``tests/_astrbot_stub.py`` 的桩真跑一遍后台检查循环，
   验「enable=false 零副作用」「session_list 为空不发」「provider 返回 None 不崩」。

覆盖范围（对应任务要求）：
- timeline 解析：正常 / 非法抛异常 / 覆盖全天 / 空隙 → blocked
- should_speak 的每条拒绝原因（各一个独立 reason 常量）
- 概率 = 档位概率 × 活跃度，activity=0 → 永不发
- 跨天状态重置
- 抖动落在 [-jitter, +jitter] 且用注入的 rng（同种子可复现）
- 配置缺键 / 写坏 → 退回安全默认（enable=false）
- enable=false 时零副作用：不解析 timeline、不起循环、不调 provider
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import random
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "life"))

from tests._astrbot_stub import install  # noqa: E402

LOGGER = install()  # 必须在 import main 之前（幂等）

from core import proactive  # noqa: E402
from life import main as plugin_main  # noqa: E402
from life import webapi  # noqa: E402

WED_0900 = datetime(2026, 10, 7, 9, 0)  # 周三 09:00 → low
WED_1100 = datetime(2026, 10, 7, 11, 0)  # normal
WED_2000 = datetime(2026, 10, 7, 20, 0)  # high
WED_0300 = datetime(2026, 10, 7, 3, 0)  # blocked
UMO = "aiocqhttp:GroupMessage:100"

DEFAULT_SLOTS = proactive.parse_timeline(proactive.DEFAULT_TIMELINE)


class _FixedRng:
    """确定性 rng：random() 永远返回同一个值，uniform 永远返回左端点。"""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.calls = 0
        self.uniform_calls: list[tuple[float, float]] = []

    def random(self) -> float:
        self.calls += 1
        return self.value

    def uniform(self, low: float, high: float) -> float:
        self.uniform_calls.append((low, high))
        return low


def cfg(**over) -> proactive.ProactiveConfig:
    """做一份「一定允许」的配置，各测试只覆盖自己关心的那一个键。"""
    base = dict(
        enable=True,
        session_list=frozenset({UMO}),
        timeline="0-24=high",
        probability=dict(proactive.DEFAULT_PROBABILITY),
        min_idle_minutes=0.0,
        cooldown_minutes=0.0,
        daily_budget=3,
        max_unanswered=2,
        jitter_minutes=0.0,
        check_interval_seconds=60.0,
    )
    base.update(over)
    return proactive.ProactiveConfig(**base)


def state(**over) -> proactive.DailyState:
    base = dict(umo=UMO)
    base.update(over)
    return proactive.DailyState(**base)


# ======================================================================
# 1. timeline 解析
# ======================================================================
class TestParseTimeline(unittest.TestCase):
    def test_default_timeline_covers_whole_day(self):
        self.assertAlmostEqual(DEFAULT_SLOTS[0].start, 0.0)
        self.assertAlmostEqual(DEFAULT_SLOTS[-1].end, 24.0)
        for before, after in zip(DEFAULT_SLOTS, DEFAULT_SLOTS[1:]):
            self.assertAlmostEqual(before.end, after.start, msg=f"{before} -> {after}")

    def test_spec_example_samples(self):
        self.assertEqual(proactive.availability_at(WED_0300, DEFAULT_SLOTS), proactive.BLOCKED)
        self.assertEqual(proactive.availability_at(WED_0900, DEFAULT_SLOTS), proactive.LOW)
        self.assertEqual(proactive.availability_at(WED_1100, DEFAULT_SLOTS), proactive.NORMAL)
        self.assertEqual(
            proactive.availability_at(datetime(2026, 10, 7, 15, 0), DEFAULT_SLOTS), proactive.LOW
        )
        self.assertEqual(proactive.availability_at(WED_2000, DEFAULT_SLOTS), proactive.HIGH)

    def test_comma_and_newline_both_accepted(self):
        comma = proactive.parse_timeline("0-12=low,12-24=high")
        lines = proactive.parse_timeline("0-12=low\n12-24=high")
        self.assertEqual([(s.start, s.end, s.level) for s in comma],
                         [(s.start, s.end, s.level) for s in lines])

    def test_comments_and_blank_lines_ignored(self):
        slots = proactive.parse_timeline("# 注释\n\n0-24=normal\n")
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].level, proactive.NORMAL)

    def test_wrap_around_split(self):
        slots = proactive.parse_timeline("22-2=high")
        self.assertAlmostEqual(slots[0].start, 0.0)
        self.assertAlmostEqual(slots[0].end, 2.0)
        self.assertEqual(slots[0].level, proactive.HIGH)
        self.assertEqual(slots[-1].level, proactive.HIGH)
        self.assertAlmostEqual(slots[-1].end, 24.0)

    def test_gap_is_filled_with_blocked(self):
        slots = proactive.parse_timeline("8-10=high")
        self.assertEqual(slots[0].level, proactive.BLOCKED)  # 0-8 没配 = 请勿打扰
        self.assertAlmostEqual(slots[0].start, 0.0)
        self.assertAlmostEqual(slots[0].end, 8.0)
        self.assertEqual(slots[-1].level, proactive.BLOCKED)  # 10-24 同理
        self.assertAlmostEqual(slots[-1].end, 24.0)

    def test_overlap_later_entry_wins(self):
        slots = proactive.parse_timeline("0-24=blocked,10-12=high")
        self.assertEqual(proactive.availability_at(WED_1100, slots), proactive.HIGH)
        self.assertEqual(proactive.availability_at(WED_0900, slots), proactive.BLOCKED)

    def test_bad_format_raises(self):
        for bad in ["0-8 blocked", "0-8=", "abc", "0-8", "0-8=high=low"]:
            with self.assertRaises(proactive.TimelineError, msg=bad):
                proactive.parse_timeline(bad)

    def test_unknown_level_raises(self):
        with self.assertRaises(proactive.TimelineError) as ctx:
            proactive.parse_timeline("0-24=busy")
        self.assertIn("busy", str(ctx.exception))

    def test_out_of_range_raises(self):
        for bad in ["25-26=high", "0-25=low", "5-5=normal", "-1-6=low"]:
            with self.assertRaises(proactive.TimelineError, msg=bad):
                proactive.parse_timeline(bad)

    def test_empty_raises(self):
        for bad in ["", "   ", "\n\n", "# 只有注释"]:
            with self.assertRaises(proactive.TimelineError, msg=repr(bad)):
                proactive.parse_timeline(bad)


class TestAvailabilityAt(unittest.TestCase):
    def test_empty_timeline_is_blocked(self):
        self.assertEqual(proactive.availability_at(WED_1100, ()), proactive.BLOCKED)
        self.assertEqual(proactive.availability_at(WED_1100, None), proactive.BLOCKED)

    def test_accepts_text_or_slots(self):
        text = "0-12=low,12-24=high"
        self.assertEqual(
            proactive.availability_at(WED_1100, text),
            proactive.availability_at(WED_1100, proactive.parse_timeline(text)),
        )

    def test_boundary_is_half_open(self):
        slots = proactive.parse_timeline("8-10=low,10-24=high")
        self.assertEqual(
            proactive.availability_at(datetime(2026, 10, 7, 7, 59, 59), slots), proactive.BLOCKED
        )
        self.assertEqual(proactive.availability_at(datetime(2026, 10, 7, 8, 0), slots), proactive.LOW)
        self.assertEqual(proactive.availability_at(datetime(2026, 10, 7, 10, 0), slots), proactive.HIGH)


GROUP_UMO = "aiocqhttp:GroupMessage:123456"
FRIEND_UMO = "aiocqhttp:FriendMessage:123456"


class TestMatchSession(unittest.TestCase):
    """白名单：完整 umo 精确匹配；裸 id 退化为「最后一段相等」（群聊 / 私聊都填得）。"""

    def test_full_umo_exact_hit(self):
        self.assertTrue(proactive.match_session(GROUP_UMO, [GROUP_UMO]))

    def test_bare_group_id_hits_group_chat(self):
        self.assertTrue(proactive.match_session(GROUP_UMO, ["123456"]))

    def test_bare_qq_id_hits_private_chat(self):
        self.assertTrue(proactive.match_session(FRIEND_UMO, ["123456"]))

    def test_same_bare_id_hits_every_session_with_that_tail(self):
        # 同一个裸 id 出现在两个会话时都要命中，不能只取一个
        entries = ["123456"]
        hits = [umo for umo in (GROUP_UMO, FRIEND_UMO) if proactive.match_session(umo, entries)]
        self.assertEqual(hits, [GROUP_UMO, FRIEND_UMO])

    def test_empty_list_never_matches(self):
        for empty in ([], (), set(), frozenset(), None, ""):
            self.assertFalse(proactive.match_session(GROUP_UMO, empty), msg=repr(empty))

    def test_case_insensitive(self):
        self.assertTrue(proactive.match_session("aiocqhttp:GroupMessage:123456",
                                                ["AIOCQHTTP:GROUPMESSAGE:123456"]))
        self.assertTrue(proactive.match_session("AIOCQHTTP:GroupMessage:123456",
                                                ["aiocqhttp:groupmessage:123456"]))

    def test_whitespace_and_int_entries(self):
        self.assertTrue(proactive.match_session(GROUP_UMO, ["  123456  "]))
        self.assertTrue(proactive.match_session(GROUP_UMO, [123456]))
        self.assertTrue(proactive.match_session("  aiocqhttp:GroupMessage:123456  ", [GROUP_UMO]))

    def test_garbage_entries_never_match_and_never_raise(self):
        garbage = ["", "   ", "这不是群号", "abc", None, ":", "aiocqhttp:GroupMessage:", 3.14]
        for umo in (GROUP_UMO, FRIEND_UMO, "", None, "没有冒号的会话名"):
            self.assertFalse(
                proactive.match_session(umo, garbage), msg=f"umo={umo!r}"
            )

    def test_full_umo_entry_does_not_leak_to_other_session_kind(self):
        """填了完整 umo 就是指定了那个会话：不再退化成「最后一段」，
        否则 ...:GroupMessage:123456 会意外命中同一个号码的私聊。"""
        self.assertFalse(proactive.match_session(FRIEND_UMO, [GROUP_UMO]))

    def test_empty_umo_never_matches(self):
        self.assertFalse(proactive.match_session("", ["123456"]))
        self.assertFalse(proactive.match_session(None, ["123456"]))

    def test_mixed_entries(self):
        entries = [GROUP_UMO, "999"]
        self.assertTrue(proactive.match_session(GROUP_UMO, entries))
        self.assertTrue(proactive.match_session("aiocqhttp:FriendMessage:999", entries))
        self.assertFalse(proactive.match_session("aiocqhttp:FriendMessage:123456", entries))


class TestCandidateSessions(unittest.TestCase):
    """候选会话：含 ``:`` 的白名单条目本身就是会话；裸 id 要靠「见过该会话」来发现。"""

    def test_full_umo_entries_are_candidates_without_seen(self):
        self.assertEqual(proactive.candidate_sessions([GROUP_UMO], set()), [GROUP_UMO])

    def test_bare_id_needs_seen_session(self):
        self.assertEqual(proactive.candidate_sessions(["123456"], set()), [])
        self.assertEqual(
            proactive.candidate_sessions(["123456"], {GROUP_UMO, FRIEND_UMO}),
            [FRIEND_UMO, GROUP_UMO],  # 字典序
        )

    def test_unrelated_seen_sessions_are_filtered_out(self):
        self.assertEqual(
            proactive.candidate_sessions(["123456"], {"aiocqhttp:GroupMessage:999"}), []
        )

    def test_sorted_and_deduped(self):
        seen = {GROUP_UMO, FRIEND_UMO, "aiocqhttp:GroupMessage:999"}
        self.assertEqual(
            proactive.candidate_sessions(["123456", "123456", GROUP_UMO], seen),
            [FRIEND_UMO, GROUP_UMO],
        )

    def test_empty_whitelist_yields_nothing(self):
        self.assertEqual(proactive.candidate_sessions([], {GROUP_UMO}), [])
        self.assertEqual(proactive.candidate_sessions(None, {GROUP_UMO}), [])


# ======================================================================
# 2. should_speak：每条拒绝原因
# ======================================================================
class TestShouldSpeakReasons(unittest.TestCase):
    def decide(self, *, now=WED_2000, st=None, config=None, rng=None, timeline=None,
               activity=1.0, sleeping=False):
        config = config if config is not None else cfg()
        if timeline is None:
            timeline = config.timeline
        return proactive.should_speak(
            now,
            st if st is not None else state(),
            config,
            rng if rng is not None else _FixedRng(0.0),
            timeline=timeline,
            activity=activity,
            sleeping=sleeping,
        )

    # ---- 拒绝：每条一个独立 reason ------------------------------------
    def test_disabled(self):
        decision = self.decide(config=cfg(enable=False))
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_DISABLED)

    def test_empty_session_list(self):
        decision = self.decide(config=cfg(session_list=frozenset()))
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_EMPTY_LIST)

    def test_not_in_session_list(self):
        decision = self.decide(
            st=state(umo="aiocqhttp:FriendMessage:999"), config=cfg(session_list={UMO})
        )
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_NOT_IN_LIST)

    def test_bare_group_id_in_whitelist_allows_group_session(self):
        decision = self.decide(
            st=state(umo=GROUP_UMO), config=cfg(session_list={"123456"}), timeline=DEFAULT_SLOTS
        )
        self.assertTrue(decision.allow)

    def test_bare_qq_id_in_whitelist_allows_private_session(self):
        decision = self.decide(
            st=state(umo=FRIEND_UMO), config=cfg(session_list={"123456"}), timeline=DEFAULT_SLOTS
        )
        self.assertTrue(decision.allow)

    def test_unnamed_session_is_not_whitelisted(self):
        decision = self.decide(st=proactive.DailyState())
        self.assertEqual(decision.reason, proactive.REASON_NOT_IN_LIST)

    def test_sleeping(self):
        decision = self.decide(sleeping=True)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_SLEEPING)

    def test_blocked_slot(self):
        decision = self.decide(now=WED_0300, timeline=DEFAULT_SLOTS)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_BLOCKED)
        self.assertEqual(decision.level, proactive.BLOCKED)

    def test_daily_budget_exhausted(self):
        decision = self.decide(st=state(sent=3), config=cfg(daily_budget=3))
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_BUDGET)

    def test_zero_budget_never_speaks(self):
        decision = self.decide(st=state(sent=0), config=cfg(daily_budget=0))
        self.assertEqual(decision.reason, proactive.REASON_BUDGET)

    def test_cooldown(self):
        now_stamp = WED_2000.timestamp()
        decision = self.decide(
            st=state(cooldown_until=now_stamp + 600), config=cfg(cooldown_minutes=45)
        )
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_COOLDOWN)

    def test_insufficient_idle(self):
        decision = self.decide(
            st=state(last_user_at=WED_2000.timestamp() - 60),
            config=cfg(min_idle_minutes=30),
        )
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_IDLE)

    def test_max_unanswered(self):
        decision = self.decide(st=state(unanswered=2), config=cfg(max_unanswered=2))
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_UNANSWERED)

    def test_max_unanswered_zero_means_no_limit(self):
        decision = self.decide(st=state(unanswered=99), config=cfg(max_unanswered=0))
        self.assertTrue(decision.allow)

    def test_probability_roll_failed(self):
        decision = self.decide(now=WED_1100, config=cfg(timeline="0-24=normal"), rng=_FixedRng(0.99))
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_PROBABILITY)
        self.assertAlmostEqual(decision.probability, 0.7)

    def test_allow(self):
        decision = self.decide(timeline=DEFAULT_SLOTS, rng=_FixedRng(0.0))
        self.assertTrue(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_ALLOW)
        self.assertEqual(decision.level, proactive.HIGH)
        self.assertIn("发送", decision.log_line())

    def test_allow_does_not_touch_state(self):
        """should_speak 只判定不记账；记账是调用方 note_sent 的事。"""
        st = state()
        self.assertTrue(self.decide(st=st).allow)
        self.assertEqual((st.sent, st.unanswered, st.cooldown_until), (0, 0, 0.0))

    def test_all_reject_reasons_are_distinct(self):
        self.assertEqual(len(proactive.REJECT_REASONS), len(set(proactive.REJECT_REASONS)))
        for reason in proactive.REJECT_REASONS:
            self.assertNotEqual(reason, proactive.REASON_ALLOW)


class TestProbabilityMath(unittest.TestCase):
    def test_probability_is_level_times_activity(self):
        decision = proactive.should_speak(
            WED_1100,
            state(),
            cfg(timeline="0-24=normal"),
            _FixedRng(0.0),
            activity=0.5,
        )
        self.assertTrue(decision.allow)
        self.assertAlmostEqual(decision.probability, 0.7 * 0.5)

    def test_activity_zero_never_allows(self):
        # 活跃度 0 = 睡眠/不活跃：无论 rng 多小都不发（概率乘数哲学）
        for _ in range(200):
            decision = proactive.should_speak(
                WED_2000, state(), cfg(timeline="0-24=high"), _FixedRng(0.0), activity=0.0
            )
            self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, proactive.REASON_PROBABILITY)

    def test_activity_is_clamped(self):
        decision = proactive.should_speak(
            WED_2000, state(), cfg(timeline="0-24=high"), _FixedRng(0.0), activity=5.0
        )
        self.assertAlmostEqual(decision.probability, 1.0)

    def test_blocked_probability_override_is_honoured(self):
        # 把 blocked 档概率调到 1.0，blocked 时段就重新参与掷骰子（两个旋钮都活着）
        decision = proactive.should_speak(
            WED_0300,
            state(),
            cfg(timeline="0-24=blocked", probability={**proactive.DEFAULT_PROBABILITY, "blocked": 1.0}),
            _FixedRng(0.0),
        )
        self.assertTrue(decision.allow)


class TestDailyState(unittest.TestCase):
    def test_cross_day_resets_counter(self):
        st = state()
        st.note_sent(WED_2000, cooldown_minutes=0)
        self.assertEqual(st.sent, 1)
        tomorrow = WED_2000 + timedelta(days=1)
        self.assertTrue(st.roll_day(tomorrow.date()))
        self.assertEqual(st.sent, 0)  # 新的一天，预算重新开始
        self.assertEqual(st.day, tomorrow.date())

    def test_same_day_does_not_reset(self):
        st = state()
        st.note_sent(WED_2000, cooldown_minutes=0)
        self.assertFalse(st.roll_day(WED_2000.date()))
        self.assertEqual(st.sent, 1)

    def test_note_sent_sets_cooldown_and_unanswered(self):
        st = state()
        st.note_sent(WED_2000, cooldown_minutes=45)
        self.assertEqual(st.last_sent_at, WED_2000.timestamp())
        self.assertEqual(st.cooldown_until, WED_2000.timestamp() + 45 * 60)
        self.assertEqual(st.unanswered, 1)

    def test_user_message_resets_unanswered_but_not_cooldown(self):
        st = state()
        st.note_sent(WED_2000, cooldown_minutes=45)
        st.note_user_message(WED_2000 + timedelta(minutes=1))
        self.assertEqual(st.unanswered, 0)
        self.assertEqual(st.cooldown_until, WED_2000.timestamp() + 45 * 60)  # 冷却不受影响

    def test_cross_day_keeps_unanswered(self):
        """刻意行为：昨天没理我，今天零点也不该立刻又开始搭话。"""
        st = state(unanswered=2)
        st.note_sent(WED_2000, cooldown_minutes=0)  # → unanswered 3
        st.roll_day((WED_2000 + timedelta(days=1)).date())
        self.assertEqual(st.sent, 0)
        self.assertEqual(st.unanswered, 3)  # 只有收到用户消息才清零，跨天不清零


class TestNextWindow(unittest.TestCase):
    def test_jitter_within_bounds(self):
        config = cfg(timeline="0-24=high", jitter_minutes=12)
        slots = proactive.parse_timeline("0-24=high")
        for seed in range(20):
            rng = random.Random(seed)
            at = proactive.next_window(WED_1100, config, rng, timeline=slots)
            base = proactive.next_speakable_start(WED_1100, slots)
            self.assertIsNotNone(at)
            delta = (at - base).total_seconds()
            self.assertGreaterEqual(delta, -12 * 60)
            self.assertLessEqual(delta, 12 * 60)

    def test_reproducible_with_same_seed(self):
        config = cfg(timeline="0-24=high", jitter_minutes=12)
        slots = proactive.parse_timeline("0-24=high")
        first = proactive.next_window(WED_1100, config, random.Random(7), timeline=slots)
        second = proactive.next_window(WED_1100, config, random.Random(7), timeline=slots)
        self.assertEqual(first, second)

    def test_zero_jitter_returns_base_exactly(self):
        config = cfg(timeline="0-24=high", jitter_minutes=0)
        slots = proactive.parse_timeline("0-24=high")
        at = proactive.next_window(WED_1100, config, _FixedRng(0.0), timeline=slots)
        self.assertEqual(at, WED_1100)

    def test_open_window_jitter_only_delays(self):
        """窗口已经开着时抖动只往后拖：结果永远落在 [now, now+jitter] 里。"""
        config = cfg(timeline="0-24=high", jitter_minutes=12)
        slots = proactive.parse_timeline("0-24=high")
        for seed in range(40):
            at = proactive.next_window(WED_1100, config, random.Random(seed), timeline=slots)
            delta = (at - WED_1100).total_seconds()
            self.assertGreaterEqual(delta, 0.0)
            self.assertLessEqual(delta, 12 * 60)

    def test_future_base_jitter_is_forward_only_and_bounded(self):
        """基准在未来（当前 blocked）时：抖动只往后，且绝不被拖进 blocked/过去。

        反向抖动会落进窗口前的 blocked 档，被夹回基准——这是刻意行为：
        「准点」比「提前到请勿打扰的时段」好。
        """
        config = cfg(timeline=proactive.DEFAULT_TIMELINE, jitter_minutes=12)
        base = datetime(2026, 10, 7, 8, 0)
        deltas = []
        for seed in range(60):
            at = proactive.next_window(WED_0300, config, random.Random(seed), timeline=DEFAULT_SLOTS)
            self.assertNotEqual(proactive.availability_at(at, DEFAULT_SLOTS), proactive.BLOCKED)
            deltas.append((at - base).total_seconds())
        self.assertTrue(all(d >= 0 for d in deltas), "绝不允许提前到窗口之外")
        self.assertLessEqual(max(deltas), 12 * 60)
        self.assertTrue(any(d > 0 for d in deltas), "至少有一次真的往后抖了")

    def test_large_jitter_cannot_cross_into_blocked(self):
        """基准在未来时，负抖动不许把候选时刻拖回 blocked 档。"""
        config = cfg(timeline=proactive.DEFAULT_TIMELINE, jitter_minutes=600)  # 10 小时
        for seed in range(30):
            at = proactive.next_window(WED_0300, config, random.Random(seed), timeline=DEFAULT_SLOTS)
            self.assertNotEqual(proactive.availability_at(at, DEFAULT_SLOTS), proactive.BLOCKED)

    def test_from_blocked_waits_for_next_slot(self):
        config = cfg(timeline=proactive.DEFAULT_TIMELINE, jitter_minutes=0)
        at = proactive.next_window(WED_0300, config, _FixedRng(0.0), timeline=DEFAULT_SLOTS)
        self.assertEqual(at, datetime(2026, 10, 7, 8, 0))  # 8:00 起是 low

    def test_all_blocked_returns_none(self):
        slots = proactive.parse_timeline("0-24=blocked")
        self.assertIsNone(proactive.next_window(WED_1100, cfg(timeline="0-24=blocked"), _FixedRng(0.0), timeline=slots))
        self.assertIsNone(proactive.next_speakable_start(WED_1100, slots))

    def test_never_in_the_past(self):
        config = cfg(timeline="0-24=high", jitter_minutes=60)
        slots = proactive.parse_timeline("0-24=high")
        for seed in range(30):
            at = proactive.next_window(WED_1100, config, random.Random(seed), timeline=slots)
            self.assertGreaterEqual(at, WED_1100)

    def test_jitter_result_never_lands_in_blocked_slot(self):
        config = cfg(timeline=proactive.DEFAULT_TIMELINE, jitter_minutes=120)
        # 20:00 是 high，下一段 23:00 变 low；抖动再大也不许排到 0-8 的 blocked 里
        for seed in range(50):
            at = proactive.next_window(WED_2000, config, random.Random(seed), timeline=DEFAULT_SLOTS)
            self.assertNotEqual(proactive.availability_at(at, DEFAULT_SLOTS), proactive.BLOCKED)
            self.assertGreaterEqual(at, WED_2000)


# ======================================================================
# 3. 配置兜底
# ======================================================================
class TestConfigFromRaw(unittest.TestCase):
    def test_missing_config_is_safe_default(self):
        config = proactive.ProactiveConfig.from_raw(None)
        self.assertFalse(config.enable)  # ★默认关闭
        self.assertFalse(config.active)
        self.assertEqual(config.timeline, proactive.DEFAULT_TIMELINE)
        self.assertEqual(config.daily_budget, 3)
        self.assertEqual(config.max_unanswered, 2)
        self.assertEqual(config.session_list, frozenset())
        self.assertAlmostEqual(config.llm_timeout_seconds, 45.0)

    def test_broken_values_fall_back(self):
        config = proactive.ProactiveConfig.from_raw(
            {
                "enable": "不是布尔",
                "session_list": 12345,
                "timeline": 42,
                "probability": "坏值",
                "min_idle_minutes": "abc",
                "cooldown_minutes": None,
                "daily_budget": "x",
                "max_unanswered": [],
                "jitter_minutes": "NaN",
                "check_interval_seconds": -5,
                "llm_timeout_seconds": 0,
            }
        )
        self.assertFalse(config.enable)
        self.assertEqual(config.session_list, frozenset({"12345"}))
        self.assertEqual(config.timeline, "42")
        self.assertEqual(config.probability, proactive.DEFAULT_PROBABILITY)
        self.assertAlmostEqual(config.min_idle_minutes, 30.0)
        self.assertAlmostEqual(config.cooldown_minutes, 45.0)
        self.assertEqual(config.daily_budget, 3)
        self.assertEqual(config.max_unanswered, 2)
        self.assertAlmostEqual(config.jitter_minutes, 12.0)
        self.assertAlmostEqual(config.check_interval_seconds, 5.0)  # 下限，避免忙等
        self.assertAlmostEqual(config.llm_timeout_seconds, 1.0)  # 下限

    def test_probability_is_clamped(self):
        config = proactive.ProactiveConfig.from_raw({"probability": {"high": 5, "low": -3}})
        self.assertEqual(config.probability["high"], 1.0)
        self.assertEqual(config.probability["low"], 0.0)

    def test_session_list_accepts_text(self):
        config = proactive.ProactiveConfig.from_raw({"session_list": "a\nb,c"})
        self.assertEqual(config.session_list, frozenset({"a", "b", "c"}))

    def test_active_requires_enable_and_list(self):
        self.assertFalse(proactive.ProactiveConfig(enable=True, session_list=frozenset()).active)
        self.assertFalse(proactive.ProactiveConfig(enable=False, session_list=frozenset({"x"})).active)
        self.assertTrue(proactive.ProactiveConfig(enable=True, session_list=frozenset({"x"})).active)


# ======================================================================
# 4. 内容处理纯函数
# ======================================================================
class TestSplitMessage(unittest.TestCase):
    def test_short_text_stays_single_part(self):
        self.assertEqual(proactive.split_message("在忙吗？"), ["在忙吗？"])

    def test_empty_yields_nothing(self):
        self.assertEqual(proactive.split_message(""), [])
        self.assertEqual(proactive.split_message("   \n "), [])
        self.assertEqual(proactive.split_message(None), [])

    def test_long_text_splits_and_keeps_bound(self):
        text = "。".join(f"第{i}句内容还挺长的" for i in range(30)) + "。"
        parts = proactive.split_message(text, max_chars=45, max_parts=10)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)  # 不丢内容、不改字符
        for part in parts[:-1]:
            self.assertLessEqual(len(part), 45)

    def test_max_parts_merges_remainder(self):
        text = "。".join(f"句子{i}" for i in range(40)) + "。"
        parts = proactive.split_message(text, max_chars=10, max_parts=3)
        self.assertEqual(len(parts), 3)
        self.assertEqual("".join(parts), text)

    def test_single_overlong_sentence_is_hard_cut(self):
        parts = proactive.split_message("啊" * 25, max_chars=10, max_parts=10)
        self.assertEqual(parts, ["啊" * 10, "啊" * 10, "啊" * 5])


class TestExtractText(unittest.TestCase):
    def test_completion_text_attr(self):
        class Resp:
            completion_text = " 你好呀 "

        self.assertEqual(proactive.extract_text(Resp()), "你好呀")

    def test_str_and_dict(self):
        self.assertEqual(proactive.extract_text(" 直接给字符串 "), "直接给字符串")
        self.assertEqual(proactive.extract_text({"completion_text": "字典"}), "字典")
        self.assertEqual(proactive.extract_text({"text": "text 键"}), "text 键")

    def test_unusable_returns_empty(self):
        self.assertEqual(proactive.extract_text(None), "")
        self.assertEqual(proactive.extract_text(object()), "")
        self.assertEqual(proactive.extract_text({"completion_text": "   "}), "")


class TestComposePrompt(unittest.TestCase):
    def test_defaults(self):
        prompt, system = proactive.compose_prompt(proactive.ProactiveConfig())
        self.assertEqual(prompt, proactive.DEFAULT_PROMPT_TEMPLATE)
        self.assertEqual(system, "")

    def test_overrides(self):
        prompt, system = proactive.compose_prompt(
            proactive.ProactiveConfig(system_prompt=" 你是猫娘 ", prompt_template="说句话")
        )
        self.assertEqual(prompt, "说句话")
        self.assertEqual(system, "你是猫娘")


# ======================================================================
# 5. main.py 接线
# ======================================================================
class FakeResp:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class FakeProvider:
    """够用的假 provider：记录每次 text_chat 的入参。"""

    def __init__(self, text: str = "在忙吗？", *, exc: Exception | None = None,
                 delay: float = 0.0, response=None) -> None:
        self.text = text
        self.exc = exc
        self.delay = delay
        self.response = response
        self.calls: list[dict] = []

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None, **kwargs):
        self.calls.append(
            {"prompt": prompt, "system_prompt": system_prompt, "contexts": contexts}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        if self.response is not None:
            return self.response
        return FakeResp(self.text)


class FakeMsgObj:
    def __init__(self) -> None:
        self.timestamp = 0.0
        self.raw_message = None


class FakeEvent:
    def __init__(self, *, umo: str = UMO, sender: str = "2001", text: str = "在吗") -> None:
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = False
        self.message_str = text
        self.message_obj = FakeMsgObj()
        self._sender = sender
        self._stopped = False
        self._result = object()

    def get_sender_id(self) -> str:
        return self._sender

    def get_self_id(self) -> str:
        return "9999"

    def is_private_chat(self) -> bool:
        return False

    def get_sender_name(self) -> str:
        return "某人"

    def get_message_str(self) -> str:
        return self.message_str

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped

    def get_result(self):
        return None if self._stopped else self._result


class FakeContext:
    def __init__(self, provider=None, *, provider_exc: Exception | None = None) -> None:
        self.sent: list[tuple[str, object]] = []
        self.registered_web_apis: list[tuple] = []
        self.provider = provider
        self.provider_exc = provider_exc
        self.provider_calls = 0

    def register_web_api(self, route, view_handler, methods, desc) -> None:
        self.registered_web_apis = [r for r in self.registered_web_apis if r[0] != route]
        self.registered_web_apis.append((route, view_handler, methods, desc))

    def get_config(self, umo: str | None = None) -> dict:
        return {}

    def get_using_provider(self, umo: str | None = None):
        self.provider_calls += 1
        if self.provider_exc is not None:
            raise self.provider_exc
        return self.provider

    async def send_message(self, session: str, message_chain) -> bool:
        self.sent.append((session, message_chain))
        return True


def make_config(**over) -> dict:
    base = {
        "enable": True,
        "time_weights": "0-24=100",  # 活跃度 1.0
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


def proactive_conf(**over) -> dict:
    """一份「一定允许」的 proactive 配置。"""
    base = {
        "enable": True,
        "session_list": [UMO],
        "timeline": "0-24=high",
        "probability": {"blocked": 0.0, "low": 0.25, "normal": 0.7, "high": 1.0},
        "min_idle_minutes": 0,
        "cooldown_minutes": 0,
        "daily_budget": 3,
        "max_unanswered": 2,
        "jitter_minutes": 0,
        "check_interval_seconds": 60,
        "system_prompt": "",
        "prompt_template": "说点什么",
        "llm_timeout_seconds": 5,
    }
    base.update(over)
    return base


class WiringTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        LOGGER.records.clear()

    async def asyncTearDown(self) -> None:
        plugin = getattr(self, "plugin", None)
        if plugin is not None:
            await plugin.terminate()

    def build(self, provider=None, *, provider_exc=None, **over) -> "plugin_main.ReplyGate":
        self.ctx = FakeContext(provider, provider_exc=provider_exc)
        self.plugin = plugin_main.ReplyGate(self.ctx, config=make_config(**over))
        return self.plugin


class TestWiringZeroSideEffects(WiringTestBase):
    """★安全红线：enable=false / session_list 为空 → 零副作用。"""

    async def test_disabled_does_nothing_at_all(self):
        calls: list[str] = []
        real_parse = proactive.parse_timeline

        def spy(text):
            calls.append(text)
            return real_parse(text)

        proactive.parse_timeline = spy  # type: ignore[assignment]
        try:
            plugin = self.build(
                proactive=proactive_conf(enable=False, timeline="这根本不是时间线!!!")
            )
            await plugin.initialize()
            await plugin._maybe_proactive_check()
            await plugin.gate(FakeEvent())
        finally:
            proactive.parse_timeline = real_parse  # type: ignore[assignment]

        self.assertEqual(calls, [], "enable=false 时不许解析 timeline")
        self.assertIsNone(plugin._proactive_task, "enable=false 时不许起循环")
        self.assertEqual(plugin._proactive_slots, ())
        self.assertEqual(self.ctx.provider_calls, 0, "enable=false 时不许调 provider")
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(plugin.stats_snapshot()["proactive_sent"], 0)

    async def test_empty_session_list_sends_nothing(self):
        plugin = self.build(proactive=proactive_conf(session_list=[]), provider=FakeProvider())
        await plugin.initialize()
        await plugin._maybe_proactive_check()
        self.assertIsNone(plugin._proactive_task)
        self.assertEqual(plugin._proactive_slots, ())
        self.assertEqual(self.ctx.provider_calls, 0)
        self.assertEqual(self.ctx.sent, [])

    async def test_missing_proactive_group_is_off(self):
        plugin = self.build(provider=FakeProvider())
        await plugin._maybe_proactive_check()
        self.assertIsNone(plugin._proactive_task)
        self.assertFalse(plugin._proactive_cfg.enable)
        self.assertEqual(self.ctx.sent, [])

    async def test_broken_timeline_falls_back_to_default(self):
        plugin = self.build(proactive=proactive_conf(timeline="坏时间线"), provider=FakeProvider())
        self.assertTrue(len(plugin._proactive_slots) > 0)
        self.assertTrue(any("timeline" in line for line in LOGGER.messages("error")))
        await plugin._maybe_proactive_check()  # 不该抛


class TestWiringSessionList(WiringTestBase):
    """白名单的两种写法在接线层的行为（群聊 / 私聊都能填）。"""

    async def test_full_umo_entry_works_without_any_message(self):
        plugin = self.build(
            FakeProvider(), proactive=proactive_conf(session_list=[GROUP_UMO])
        )
        await plugin._maybe_proactive_check()
        self.assertEqual([umo for umo, _ in self.ctx.sent], [GROUP_UMO])

    async def test_bare_group_id_only_after_seeing_that_session(self):
        plugin = self.build(FakeProvider(), proactive=proactive_conf(session_list=["123456"]))

        # 没收到过任何消息 → 枚举不出会话，什么都不发（裸 id 的代价）
        await plugin._maybe_proactive_check()
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(self.ctx.provider_calls, 0)

        # 收到一条群消息 → 记录该会话 → 下一次检查就能主动开口，
        # 而且必须发到**真实 umo**上，绝不能发到 "123456"
        await plugin.gate(FakeEvent(umo=GROUP_UMO))
        self.assertEqual(plugin._proactive_seen, {GROUP_UMO})
        await plugin._maybe_proactive_check()
        self.assertEqual([umo for umo, _ in self.ctx.sent], [GROUP_UMO])

    async def test_bare_qq_id_works_for_private_chat(self):
        plugin = self.build(FakeProvider(), proactive=proactive_conf(session_list=["123456"]))
        await plugin.gate(FakeEvent(umo=FRIEND_UMO))
        await plugin._maybe_proactive_check()
        self.assertEqual([umo for umo, _ in self.ctx.sent], [FRIEND_UMO])

    async def test_same_bare_id_in_two_sessions_both_get_messages(self):
        plugin = self.build(FakeProvider(), proactive=proactive_conf(session_list=["123456"]))
        await plugin.gate(FakeEvent(umo=GROUP_UMO))
        await plugin.gate(FakeEvent(umo=FRIEND_UMO))
        await plugin._maybe_proactive_check()
        self.assertEqual([umo for umo, _ in self.ctx.sent], sorted([GROUP_UMO, FRIEND_UMO]))

    async def test_non_matching_session_is_not_recorded(self):
        plugin = self.build(FakeProvider(), proactive=proactive_conf(session_list=["999"]))
        await plugin.gate(FakeEvent(umo=GROUP_UMO))
        self.assertEqual(plugin._proactive_seen, set())
        await plugin._maybe_proactive_check()
        self.assertEqual(self.ctx.sent, [])

    async def test_disabled_plugin_does_not_record_sessions(self):
        plugin = self.build(
            FakeProvider(), proactive=proactive_conf(enable=False, session_list=["123456"])
        )
        await plugin.gate(FakeEvent(umo=GROUP_UMO))
        self.assertEqual(plugin._proactive_seen, set())
        self.assertEqual(plugin._recent, {})


class TestWiringHappyPath(WiringTestBase):
    async def test_allowed_check_generates_and_sends(self):
        provider = FakeProvider("在忙吗？")
        plugin = self.build(provider, proactive=proactive_conf())
        await plugin._maybe_proactive_check()

        self.assertEqual(self.ctx.provider_calls, 1)
        self.assertEqual(len(self.ctx.sent), 1)
        session, chain = self.ctx.sent[0]
        self.assertEqual(session, UMO)
        self.assertEqual(chain.chain[0].text, "在忙吗？")
        self.assertEqual(plugin.stats_snapshot()["proactive_sent"], 1)
        self.assertEqual(plugin._proactive_states[UMO].sent, 1)
        self.assertEqual(plugin._proactive_states[UMO].unanswered, 1)
        self.assertTrue(any("主动开口已发送" in line for line in LOGGER.messages()))

    async def test_decision_is_logged_with_level_and_probability(self):
        plugin = self.build(FakeProvider(), proactive=proactive_conf())
        await plugin._maybe_proactive_check()
        line = next(l for l in LOGGER.messages() if "主动决策" in l)
        self.assertIn(UMO, line)
        self.assertIn("档位=high", line)
        self.assertIn("P=1.000", line)
        self.assertIn("allow", line)
        self.assertIn("今日已发 0/3", line)

    async def test_system_prompt_and_recent_chat_are_passed(self):
        provider = FakeProvider("接着聊")
        plugin = self.build(
            provider, proactive=proactive_conf(system_prompt="你是猫娘", prompt_template="说句话")
        )
        await plugin.gate(FakeEvent(text="今天好累"))  # 记进「最近聊天」
        await plugin._maybe_proactive_check()

        call = provider.calls[0]
        self.assertEqual(call["prompt"], "说句话")
        self.assertEqual(call["system_prompt"], "你是猫娘")
        self.assertEqual(call["contexts"], [{"role": "user", "content": "今天好累"}])

    async def test_empty_system_prompt_becomes_none(self):
        provider = FakeProvider()
        plugin = self.build(provider, proactive=proactive_conf(system_prompt=""))
        await plugin._maybe_proactive_check()
        self.assertIsNone(provider.calls[0]["system_prompt"])

    async def test_max_unanswered_stops_the_second_attempt(self):
        plugin = self.build(FakeProvider(), proactive=proactive_conf(max_unanswered=1))
        await plugin._maybe_proactive_check()
        self.assertEqual(len(self.ctx.sent), 1)
        await plugin._maybe_proactive_check()
        self.assertEqual(len(self.ctx.sent), 1)  # 连发一条没人理，停手
        self.assertTrue(any("max_unanswered" in line for line in LOGGER.messages()))

    async def test_daily_budget_stops_after_limit(self):
        plugin = self.build(FakeProvider(), proactive=proactive_conf(daily_budget=2, max_unanswered=0))
        for _ in range(4):
            await plugin._maybe_proactive_check()
        self.assertEqual(len(self.ctx.sent), 2)
        self.assertTrue(any("daily_budget_exhausted" in line for line in LOGGER.messages()))

    async def test_sleeping_never_sends(self):
        plugin = self.build(
            FakeProvider(), time_weights="0-24=0", proactive=proactive_conf()
        )
        await plugin._maybe_proactive_check()
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(self.ctx.provider_calls, 0)
        self.assertTrue(any("sleeping" in line for line in LOGGER.messages()))

    async def test_activity_scales_probability_zero_activity_never_sends(self):
        # 权重 1/100 → 活跃度 0.01（不睡眠），high 档概率 1.0 → P=0.01；
        # 把 rng 固定成 0.5 保证确定性失败
        plugin = self.build(
            FakeProvider(), time_weights="0-24=1", proactive=proactive_conf()
        )
        plugin._rng = _FixedRng(0.5)
        await plugin._maybe_proactive_check()
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(self.ctx.provider_calls, 0)
        self.assertTrue(any("probability" in line for line in LOGGER.messages()))

    async def test_short_content_is_discarded(self):
        plugin = self.build(FakeProvider("。"), proactive=proactive_conf())
        await plugin._maybe_proactive_check()
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(plugin.stats_snapshot()["proactive_sent"], 0)
        self.assertEqual(plugin._proactive_states[UMO].sent, 0)  # 没发出去就不算预算
        self.assertTrue(any("过短" in line for line in LOGGER.messages()))

    async def test_long_content_is_split_into_segments(self):
        real_chars, real_delay = (
            plugin_main.PROACTIVE_SEGMENT_MAX_CHARS,
            plugin_main.PROACTIVE_SEGMENT_DELAY_SECONDS,
        )
        plugin_main.PROACTIVE_SEGMENT_MAX_CHARS = 6
        plugin_main.PROACTIVE_SEGMENT_DELAY_SECONDS = 0  # 测试里不真睡
        try:
            plugin = self.build(
                FakeProvider("第一句话。第二句话。第三句话。"), proactive=proactive_conf()
            )
            await plugin._maybe_proactive_check()
        finally:
            plugin_main.PROACTIVE_SEGMENT_MAX_CHARS = real_chars
            plugin_main.PROACTIVE_SEGMENT_DELAY_SECONDS = real_delay

        self.assertGreater(len(self.ctx.sent), 1)
        texts = [chain.chain[0].text for _, chain in self.ctx.sent]
        self.assertEqual("".join(texts), "第一句话。第二句话。第三句话。")

    async def test_cooldown_end_is_jittered(self):
        """计划时刻带抖动：冷却结束 ∈ [准点, 准点 + jitter]，真人不会准点。"""
        plugin = self.build(
            FakeProvider(), proactive=proactive_conf(cooldown_minutes=45, jitter_minutes=12)
        )
        offsets = []
        for seed in range(10):
            plugin._rng = random.Random(seed)
            plugin._proactive_states.clear()
            await plugin._maybe_proactive_check()
            st = plugin._proactive_states[UMO]
            offsets.append(st.cooldown_until - (st.last_sent_at + 45 * 60))
        for offset in offsets:
            self.assertGreaterEqual(offset, 0.0)
            self.assertLessEqual(offset, 12 * 60)
        self.assertTrue(any(o > 0 for o in offsets), "至少有一次真的抖动了")

    async def test_second_session_is_checked_after_first(self):
        other = "aiocqhttp:GroupMessage:200"
        plugin = self.build(
            FakeProvider(), proactive=proactive_conf(session_list=[UMO, other])
        )
        await plugin._maybe_proactive_check()
        self.assertEqual([umo for umo, _ in self.ctx.sent], [UMO, other])
        self.assertEqual(plugin.stats_snapshot()["proactive_sent"], 2)


class TestWiringProviderFailures(WiringTestBase):
    """provider 拿不到 / 失败 / 超时 / 空回复 → 跳过，不崩、不重试、不阻塞。"""

    async def test_provider_none_warns_and_skips(self):
        plugin = self.build(None, proactive=proactive_conf())
        await plugin._maybe_proactive_check()
        await plugin._maybe_proactive_check()  # 再来一次也不许崩
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(plugin.stats_snapshot()["proactive_sent"], 0)
        warnings = [l for l in LOGGER.messages("warning") if "provider" in l]
        self.assertEqual(len(warnings), 1, "同一类告警只打一次（_warn_once）")

    async def test_get_using_provider_raises_is_swallowed(self):
        plugin = self.build(provider_exc=RuntimeError("没有 provider"), proactive=proactive_conf())
        await plugin._maybe_proactive_check()
        self.assertEqual(self.ctx.sent, [])
        self.assertTrue(any("取 provider 失败" in l for l in LOGGER.messages("exception")))

    async def test_text_chat_raises_is_swallowed(self):
        plugin = self.build(
            FakeProvider(exc=RuntimeError("provider 炸了")), proactive=proactive_conf()
        )
        await plugin._maybe_proactive_check()
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(plugin._proactive_states[UMO].sent, 0)
        self.assertTrue(any("LLM 调用失败" in l for l in LOGGER.messages("exception")))

    async def test_text_chat_timeout_is_swallowed(self):
        plugin = self.build(FakeProvider(delay=5.0), proactive=proactive_conf())
        # 配置下限是 1 秒；测试里把超时替换成 0.05 秒，避免真等
        plugin._proactive_cfg = dataclasses.replace(plugin._proactive_cfg, llm_timeout_seconds=0.05)
        started = time.monotonic()
        await plugin._maybe_proactive_check()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "超时必须立刻放弃，绝不阻塞")
        self.assertEqual(self.ctx.sent, [])
        self.assertTrue(any("LLM 超时" in l for l in LOGGER.messages("warning")))

    async def test_empty_response_is_skipped(self):
        plugin = self.build(
            FakeProvider(response=object()), proactive=proactive_conf()
        )
        await plugin._maybe_proactive_check()
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(plugin._proactive_states[UMO].sent, 0)

    async def test_send_failure_is_swallowed(self):
        plugin = self.build(FakeProvider(), proactive=proactive_conf())

        async def boom(session, chain):
            raise RuntimeError("发送挂了")

        self.ctx.send_message = boom  # type: ignore[assignment]
        await plugin._maybe_proactive_check()
        self.assertEqual(plugin.stats_snapshot()["proactive_sent"], 0)
        self.assertTrue(any("发送失败" in l for l in LOGGER.messages("exception")))


class TestWiringLoop(WiringTestBase):
    async def test_loop_starts_only_when_active(self):
        plugin = self.build(proactive=proactive_conf(), provider=FakeProvider())
        await plugin.initialize()
        self.assertIsNotNone(plugin._proactive_task)
        await plugin.terminate()
        self.assertIsNone(plugin._proactive_task)

    async def test_loop_is_cancelled_on_terminate(self):
        plugin = self.build(proactive=proactive_conf(), provider=FakeProvider())
        await plugin.initialize()
        task = plugin._proactive_task
        await asyncio.sleep(0)  # 让循环真正跑起来并停在 sleep 上
        self.assertFalse(task.done())
        await plugin.terminate()
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())

    async def test_config_toggle_stops_and_restarts_loop(self):
        plugin = self.build(proactive=proactive_conf(), provider=FakeProvider())
        await plugin.initialize()
        self.assertIsNotNone(plugin._proactive_task)

        plugin.config["proactive"] = proactive_conf(enable=False)
        plugin._apply_config()  # 页面保存后的路径
        self.assertIsNone(plugin._proactive_task)
        self.assertEqual(plugin._proactive_slots, ())

        plugin.config["proactive"] = proactive_conf()
        plugin._apply_config()
        self.assertIsNotNone(plugin._proactive_task)

    async def test_loop_survives_check_exception(self):
        plugin = self.build(proactive=proactive_conf(), provider=FakeProvider())
        calls: list[int] = []
        real = plugin._maybe_proactive_check

        async def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("检查炸了")
            await real()

        plugin._maybe_proactive_check = flaky  # type: ignore[assignment]
        real_interval = plugin_main.PROACTIVE_MIN_INTERVAL_SECONDS
        plugin_main.PROACTIVE_MIN_INTERVAL_SECONDS = 0.02
        plugin._proactive_cfg = dataclasses.replace(
            plugin._proactive_cfg, check_interval_seconds=0.02
        )
        try:
            task = asyncio.create_task(plugin._proactive_loop())
            await asyncio.sleep(0.15)
            # 第一轮抛异常后循环睡 60 秒重试——但任务必须还活着（绝不让循环死掉）
            self.assertFalse(task.done())
            self.assertTrue(any("主动开口循环异常" in l for l in LOGGER.messages("exception")))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            plugin_main.PROACTIVE_MIN_INTERVAL_SECONDS = real_interval


class TestStatsAndSchema(WiringTestBase):
    async def test_stats_snapshot_adds_metric_without_touching_others(self):
        plugin = self.build(proactive=proactive_conf(), provider=FakeProvider())
        snapshot = plugin.stats_snapshot()
        for key in ("drops", "allows", "queued", "flushed", "batched"):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["proactive_sent"], 0)
        await plugin._maybe_proactive_check()
        self.assertEqual(plugin.stats_snapshot()["proactive_sent"], 1)
        self.assertEqual(plugin.stats_snapshot()["drops"], 0)

    def test_schema_has_proactive_group_with_safe_defaults(self):
        schema = json.loads(
            (REPO_ROOT / "life" / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("proactive", schema)
        group = schema["proactive"]
        self.assertEqual(group["type"], "object")
        items = group["items"]
        expected = {
            "enable", "session_list", "timeline", "probability", "min_idle_minutes",
            "cooldown_minutes", "daily_budget", "max_unanswered", "jitter_minutes",
            "check_interval_seconds", "system_prompt", "prompt_template",
            "llm_timeout_seconds",
        }
        self.assertEqual(set(items), expected)
        self.assertIs(items["enable"]["default"], False)  # ★默认关闭
        self.assertEqual(items["session_list"]["default"], [])
        self.assertEqual(items["daily_budget"]["default"], 3)
        for level in proactive.LEVELS:
            self.assertIn(level, items["probability"]["items"])
        # 白名单要写成「群聊 / 私聊都能填」，并且默认关闭是两道保险
        self.assertIn("群号", items["session_list"]["description"])
        self.assertIn("QQ", items["session_list"]["description"])
        self.assertIn("留空", items["session_list"]["description"])
        self.assertIn("两道保险", items["enable"]["description"])

    def test_webapi_settings_whitelist_contains_proactive(self):
        self.assertIn("proactive", webapi.SETTINGS_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
