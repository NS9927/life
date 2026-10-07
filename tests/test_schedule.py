"""schedule.py 单测：时段权重、插值、周末系数、睡眠判定。

先跑这个（设计文档第六节第 2 步）：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot_plugin_reply_gate"))

from core import schedule  # noqa: E402

WED_15 = datetime(2026, 10, 7, 15, 0)  # 周三
SAT_15 = datetime(2026, 10, 10, 15, 0)  # 周六
WED_0300 = datetime(2026, 10, 7, 3, 0)
WED_0900 = datetime(2026, 10, 7, 9, 0)
SAT_0830 = datetime(2026, 10, 10, 8, 30)
WED_0830 = datetime(2026, 10, 7, 8, 30)


class TestParse(unittest.TestCase):
    def test_default_table(self):
        segs = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)
        self.assertEqual(len(segs), 8)
        self.assertEqual([(s.start, s.end, s.weight) for s in segs[:3]],
                         [(0.0, 2.0, 80.0), (2.0, 8.0, 0.0), (8.0, 10.0, 10.0)])
        self.assertEqual(segs[-1].start, 23.0)
        self.assertEqual(segs[-1].end, 24.0)
        self.assertEqual(segs[-1].weight, 60.0)

    def test_contiguous_and_covers_whole_day(self):
        segs = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)
        self.assertAlmostEqual(segs[0].start, 0.0)
        self.assertAlmostEqual(segs[-1].end, 24.0)
        for before, after in zip(segs, segs[1:]):
            self.assertAlmostEqual(before.end, after.start, msg=f"{before} -> {after}")

    def test_comments_and_blank_lines_ignored(self):
        segs = schedule.parse_time_weights("# 注释\n\n0-24=50\n")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].weight, 50.0)

    def test_wrap_around_split(self):
        segs = schedule.parse_time_weights("22-2=30\n2-22=80")
        self.assertAlmostEqual(segs[0].start, 0.0)
        self.assertAlmostEqual(segs[0].end, 2.0)
        self.assertEqual(segs[0].weight, 30.0)
        self.assertEqual(segs[-1].start, 22.0)
        self.assertEqual(segs[-1].weight, 30.0)

    def test_gap_filled(self):
        segs = schedule.parse_time_weights("8-10=10")
        self.assertAlmostEqual(segs[0].start, 0.0)
        self.assertAlmostEqual(segs[0].end, 8.0)
        self.assertEqual(segs[0].weight, 10.0)
        self.assertAlmostEqual(segs[-1].end, 24.0)

    def test_bad_line_raises(self):
        for bad in ["0-2 80", "0-2=abc", "0-2", "abc", ""]:
            with self.assertRaises(schedule.TimeWeightsError, msg=bad):
                schedule.parse_time_weights(bad)
        with self.assertRaises(schedule.TimeWeightsError):
            schedule.parse_time_weights("0-2=80\n坏行=10")

    def test_out_of_range_raises(self):
        for bad in ["25-26=10", "0-2=150", "5-5=10"]:
            with self.assertRaises(schedule.TimeWeightsError, msg=bad):
                schedule.parse_time_weights(bad)


class TestWeightAt(unittest.TestCase):
    def setUp(self):
        self.segs = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)

    def test_inside_segment(self):
        self.assertEqual(schedule.weight_at(1.0, self.segs), 80.0)  # 0-2 段
        self.assertEqual(schedule.weight_at(5.0, self.segs), 0.0)  # 2-8 段，睡眠
        self.assertEqual(schedule.weight_at(15.0, self.segs), 30.0)
        self.assertEqual(schedule.weight_at(20.0, self.segs), 80.0)

    def test_cut_is_midpoint(self):
        # 2:00 是 80 -> 0 的切点，插值窗口内正中应为 40
        self.assertAlmostEqual(schedule.weight_at(2.0, self.segs), 40.0)
        # 19:00 是 30 -> 80 的切点
        self.assertAlmostEqual(schedule.weight_at(19.0, self.segs), 55.0)

    def test_window_edges_hit_endpoint_weights(self):
        self.assertAlmostEqual(schedule.weight_at(2.0 - 0.75, self.segs), 80.0)  # 1:15
        self.assertAlmostEqual(schedule.weight_at(2.0 + 0.75, self.segs), 0.0)  # 2:45
        self.assertAlmostEqual(schedule.weight_at(19.0 - 0.75, self.segs), 30.0)
        self.assertAlmostEqual(schedule.weight_at(19.0 + 0.75, self.segs), 80.0)

    def test_monotonic_across_cut(self):
        # 从 18:15 到 19:45 权重必须单调不回退（平滑过渡，不出现台阶反复）
        values = [schedule.weight_at(18.25 + i * 0.05, self.segs) for i in range(25)]
        for a, b in zip(values, values[1:]):
            self.assertLessEqual(a, b + 1e-9, msg=f"{values}")

    def test_interpolation_disabled_is_hard_switch(self):
        self.assertEqual(schedule.weight_at(1.99, self.segs, interpolate_minutes=0), 80.0)
        self.assertEqual(schedule.weight_at(2.01, self.segs, interpolate_minutes=0), 0.0)

    def test_circular_interpolation_across_midnight(self):
        # 23-24=60 与 0-2=80 之间在 0 点也有切点，23:59 应落在 60~80 之间
        value = schedule.weight_at(23.98, self.segs)
        self.assertGreater(value, 60.0)
        self.assertLess(value, 80.0)

    def test_hours_wrapped(self):
        self.assertEqual(schedule.weight_at(25.0, self.segs), schedule.weight_at(1.0, self.segs))


class TestSleep(unittest.TestCase):
    def setUp(self):
        self.segs = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)

    def test_raw_zero_only(self):
        self.assertTrue(schedule.is_sleep(3.0, self.segs))
        self.assertTrue(schedule.is_sleep(7.9, self.segs))
        self.assertFalse(schedule.is_sleep(9.0, self.segs))
        self.assertFalse(schedule.is_sleep(15.0, self.segs))

    def test_boundary_is_not_sleep_even_though_weight_below_ten(self):
        # 8:00 之后权重是 10，但原始段已经不是 0，不该判成睡着
        self.assertFalse(schedule.is_sleep(8.0, self.segs))

    def test_weekday_vs_weekend(self):
        self.assertTrue(schedule.sleeping(WED_0300, self.segs))
        self.assertFalse(schedule.sleeping(WED_0900, self.segs))
        # 周六睡眠窗口后移 1 小时：2-8 变成 3-9，所以 8:30 还在睡
        self.assertTrue(schedule.sleeping(SAT_0830, self.segs))
        self.assertFalse(schedule.sleeping(WED_0830, self.segs))


class TestActivity(unittest.TestCase):
    def setUp(self):
        self.segs = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)

    def test_weekday_value(self):
        self.assertAlmostEqual(schedule.activity_ratio(WED_15, self.segs), 0.30)
        self.assertAlmostEqual(schedule.activity_ratio(WED_0300, self.segs), 0.0)
        self.assertAlmostEqual(schedule.activity_ratio(datetime(2026, 10, 7, 20, 0), self.segs), 0.80)

    def test_weekend_multiplier(self):
        self.assertAlmostEqual(schedule.activity_ratio(SAT_15, self.segs), 0.30 * 1.3)
        self.assertAlmostEqual(
            schedule.activity_ratio(SAT_15, self.segs, weekend_multiplier=1.0), 0.30
        )

    def test_clamped_to_one(self):
        # 80 × 1.3 = 104 → 必须夹到 1.0，否则概率乘数会 >1
        self.assertEqual(schedule.activity_ratio(datetime(2026, 10, 10, 20, 0), self.segs), 1.0)

    def test_zero_stays_zero_on_weekend(self):
        # 睡眠是真正的 0，不该被周末系数抬起来
        self.assertEqual(schedule.activity_ratio(datetime(2026, 10, 10, 5, 0), self.segs), 0.0)

    def test_describe(self):
        line = schedule.describe(WED_15, self.segs)
        self.assertIn("15:00", line)
        self.assertIn("0.30", line)
        self.assertIn("工作日", line)
        self.assertIn("睡眠", schedule.describe(WED_0300, self.segs))


class TestShiftSleep(unittest.TestCase):
    def setUp(self):
        self.segs = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)

    def test_shift_moves_zero_window(self):
        shifted = schedule.shift_sleep(self.segs, 1)
        zero = [s for s in shifted if s.weight == 0]
        self.assertEqual(len(zero), 1)
        self.assertAlmostEqual(zero[0].start, 3.0)
        self.assertAlmostEqual(zero[0].end, 9.0)

    def test_shift_keeps_contiguous_coverage(self):
        shifted = schedule.shift_sleep(self.segs, 1)
        self.assertAlmostEqual(shifted[0].start, 0.0)
        self.assertAlmostEqual(shifted[-1].end, 24.0)
        for before, after in zip(shifted, shifted[1:]):
            self.assertAlmostEqual(before.end, after.start)

    def test_shift_zero_is_noop(self):
        self.assertEqual(schedule.shift_sleep(self.segs, 0), self.segs)

    def test_shift_clamped_within_day(self):
        # 默认表下最多挪 2 小时（再往后就把 8-10 那段吃掉了）
        shifted = schedule.shift_sleep(self.segs, 99)
        zero = [s for s in shifted if s.weight == 0]
        self.assertEqual(len(zero), 1)
        self.assertAlmostEqual(zero[0].start, 4.0)
        self.assertAlmostEqual(zero[0].end, 10.0)
        self.assertAlmostEqual(shifted[0].start, 0.0)
        self.assertAlmostEqual(shifted[-1].end, 24.0)
        for seg in shifted:
            self.assertLess(seg.start, seg.end)
            self.assertGreaterEqual(seg.start, 0.0)
            self.assertLessEqual(seg.end, 24.0)

    def test_shift_earlier_allowed(self):
        shifted = schedule.shift_sleep(self.segs, -2)
        zero = [s for s in shifted if s.weight == 0]
        self.assertAlmostEqual(zero[0].start, 0.0)
        self.assertAlmostEqual(zero[0].end, 6.0)

    def test_shift_earlier_clamped_at_zero(self):
        shifted = schedule.shift_sleep(self.segs, -99)
        zero = [s for s in shifted if s.weight == 0]
        self.assertAlmostEqual(zero[0].start, 0.0)
        self.assertAlmostEqual(shifted[-1].end, 24.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
