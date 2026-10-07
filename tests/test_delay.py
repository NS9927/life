"""delay.py 单测：延迟区间、随活跃度变化、睡眠额外、配置容错。"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "life"))

from core.delay import DelayConfig, reply_delay_seconds  # noqa: E402


def cfg(**kw) -> DelayConfig:
    base = {"enable": True, "min_seconds": 1.0, "max_seconds": 30.0, "sleep_extra_seconds": 20.0}
    base.update(kw)
    return DelayConfig(**base)


class TestReplyDelay(unittest.TestCase):
    def test_disabled_returns_zero(self):
        self.assertEqual(reply_delay_seconds(0.5, config=cfg(enable=False)), 0.0)

    def test_activity_one_stays_in_base_range(self):
        rng = random.Random(42)
        for _ in range(200):
            value = reply_delay_seconds(1.0, config=cfg(), rng=rng)
            self.assertGreaterEqual(value, 1.0)
            self.assertLessEqual(value, 30.0 + 1e-9)

    def test_low_activity_pushes_delay_up(self):
        rng = random.Random(7)
        # span=29、系数 0.35 → 额外最多 10.15 秒
        for _ in range(200):
            value = reply_delay_seconds(0.0, config=cfg(), rng=rng)
            self.assertGreaterEqual(value, 11.1)
            self.assertLessEqual(value, 40.2)

    def test_zero_penalty_makes_delay_independent_of_activity(self):
        for seed in range(10):
            low = reply_delay_seconds(0.0, config=cfg(activity_penalty=0.0), rng=random.Random(seed))
            high = reply_delay_seconds(1.0, config=cfg(activity_penalty=0.0), rng=random.Random(seed))
            self.assertAlmostEqual(low, high)

    def test_default_config_is_conservative_after_live_tuning(self):
        # 真机上 21~50 秒的延迟被群里抱怨了，默认值必须收回来
        c = DelayConfig()
        worst = reply_delay_seconds(0.0, config=c, rng=random.Random(0))
        self.assertLessEqual(c.max_seconds, 8.0)
        self.assertLessEqual(worst, 8.0 + 8.0 * 0.35 + 1e-9)

    def test_same_seed_is_monotonic_in_activity(self):
        # 活跃度越低延迟越长（同种子对比，排除随机噪声）
        for seed in range(20):
            low = reply_delay_seconds(0.2, config=cfg(), rng=random.Random(seed))
            high = reply_delay_seconds(0.9, config=cfg(), rng=random.Random(seed))
            self.assertGreaterEqual(low, high, msg=f"seed={seed}")

    def test_sleeping_adds_extra(self):
        awake = reply_delay_seconds(0.5, config=cfg(), rng=random.Random(3))
        asleep = reply_delay_seconds(0.5, config=cfg(), sleeping=True, rng=random.Random(3))
        self.assertAlmostEqual(asleep - awake, 20.0)

    def test_zero_span_config(self):
        value = reply_delay_seconds(0.3, config=cfg(min_seconds=5.0, max_seconds=5.0))
        self.assertAlmostEqual(value, 5.0)

    def test_never_negative(self):
        value = reply_delay_seconds(1.0, config=cfg(min_seconds=0.0, max_seconds=0.0), sleeping=False)
        self.assertEqual(value, 0.0)


class TestDelayConfig(unittest.TestCase):
    def test_defaults_on_none(self):
        c = DelayConfig.from_raw(None)
        self.assertTrue(c.enable)
        self.assertEqual(c.min_seconds, 1.0)
        self.assertEqual(c.max_seconds, 8.0)
        self.assertEqual(c.activity_penalty, 0.35)

    def test_reads_dict(self):
        c = DelayConfig.from_raw(
            {"enable": False, "min_seconds": 2, "max_seconds": 10, "sleep_extra_seconds": 5}
        )
        self.assertFalse(c.enable)
        self.assertEqual(c.min_seconds, 2.0)
        self.assertEqual(c.max_seconds, 10.0)
        self.assertEqual(c.sleep_extra_seconds, 5.0)

    def test_reversed_min_max_swapped(self):
        c = DelayConfig.from_raw({"min_seconds": 30, "max_seconds": 5})
        self.assertEqual(c.min_seconds, 5.0)
        self.assertEqual(c.max_seconds, 30.0)

    def test_broken_values_fall_back(self):
        c = DelayConfig.from_raw({"min_seconds": "马上", "max_seconds": None, "enable": "关"})
        self.assertEqual(c.min_seconds, 1.0)  # 默认值
        self.assertEqual(c.max_seconds, 8.0)
        self.assertFalse(c.enable)

    def test_bool_from_strings(self):
        for truthy in ("true", "1", "yes", "是", "开", True, 1):
            self.assertTrue(DelayConfig.from_raw({"enable": truthy}).enable, msg=repr(truthy))
        for falsy in ("false", "0", "no", "否", "关", False, 0):
            self.assertFalse(DelayConfig.from_raw({"enable": falsy}).enable, msg=repr(falsy))

    def test_nan_and_inf_rejected(self):
        c = DelayConfig.from_raw({"min_seconds": "nan", "max_seconds": "inf"})
        self.assertEqual(c.min_seconds, 1.0)
        self.assertEqual(c.max_seconds, 8.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
