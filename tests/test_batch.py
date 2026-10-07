"""batch.py 单测：按活跃度分档的「攒一波统一回」调度。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot_plugin_reply_gate"))

from core import batch  # noqa: E402
from core.batch import BatchPlanner, compose_batch_text  # noqa: E402

UMO = "aiocqhttp:GroupMessage:100"
OTHER = "aiocqhttp:GroupMessage:200"


def planner(**kw) -> BatchPlanner:
    base = {
        "enabled": True,
        "window_seconds": 120.0,
        "immediate_threshold": 0.6,
        "max_messages": 10,
    }
    base.update(kw)
    return BatchPlanner(**base)


class TestDecide(unittest.TestCase):
    def test_high_activity_is_immediate(self):
        p = planner()
        decision = p.decide(UMO, "在吗", activity=0.8, now=1000.0)
        self.assertEqual(decision.action, batch.ACTION_IMMEDIATE)
        self.assertFalse(p.active(UMO))

    def test_threshold_is_inclusive(self):
        p = planner(immediate_threshold=0.6)
        self.assertEqual(p.decide(UMO, "x", 0.6, 1.0).action, batch.ACTION_IMMEDIATE)

    def test_low_activity_becomes_leader(self):
        p = planner(window_seconds=90.0)
        decision = p.decide(UMO, "第一条", activity=0.3, now=1000.0)
        self.assertEqual(decision.action, batch.ACTION_LEAD)
        self.assertAlmostEqual(decision.wait_seconds, 90.0)
        self.assertTrue(p.active(UMO))

    def test_followups_fold_into_the_batch(self):
        p = planner()
        p.decide(UMO, "第一条", 0.3, 1000.0)
        second = p.decide(UMO, "第二条", 0.3, 1005.0)
        third = p.decide(UMO, "第三条", 0.3, 1010.0)
        self.assertTrue(second.is_fold)
        self.assertTrue(third.is_fold)
        self.assertEqual(third.buffered, 3)

    def test_stale_batch_starts_a_new_one(self):
        p = planner(window_seconds=60.0)
        p.decide(UMO, "第一条", 0.3, 1000.0)
        late = p.decide(UMO, "很久以后", 0.3, 1200.0)
        self.assertEqual(late.action, batch.ACTION_LEAD)

    def test_batches_are_per_session(self):
        p = planner()
        p.decide(UMO, "A", 0.3, 1000.0)
        other = p.decide(OTHER, "B", 0.3, 1001.0)
        self.assertEqual(other.action, batch.ACTION_LEAD)
        self.assertEqual(p.pending(), {UMO: 1, OTHER: 1})

    def test_disabled_always_immediate(self):
        p = planner(enabled=False)
        self.assertEqual(p.decide(UMO, "x", 0.0, 1.0).action, batch.ACTION_IMMEDIATE)

    def test_finish_merges_and_clears(self):
        p = planner()
        p.decide(UMO, "第一条", 0.3, 1000.0)
        p.decide(UMO, "第二条", 0.3, 1001.0)
        merged = p.finish(UMO)
        self.assertIn("第一条", merged)
        self.assertIn("第二条", merged)
        self.assertFalse(p.active(UMO))
        self.assertEqual(p.finish(UMO), "")

    def test_buffered_and_drop(self):
        p = planner()
        p.decide(UMO, "第一条", 0.3, 1000.0)
        p.decide(UMO, "第二条", 0.3, 1001.0)
        self.assertEqual(p.buffered(UMO), 2)
        p.drop(UMO)
        self.assertEqual(p.buffered(UMO), 0)


class TestCompose(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(compose_batch_text([]), "")
        self.assertEqual(compose_batch_text(["", "   "]), "")

    def test_join_in_order(self):
        self.assertEqual(compose_batch_text(["一", "二", "三"]), "一\n二\n三")

    def test_keeps_newest_when_too_many(self):
        text = compose_batch_text([f"m{i}" for i in range(12)], max_messages=3)
        self.assertIn("前面还有 9 条", text)
        self.assertIn("m11", text)
        self.assertNotIn("m8", text)

    def test_whitespace_collapsed(self):
        self.assertEqual(compose_batch_text(["第一行\n第二行"]), "第一行 第二行")

    def test_char_cap(self):
        text = compose_batch_text(["x" * 5000], max_chars=100)
        self.assertEqual(len(text), 101)  # 100 + 省略号
        self.assertTrue(text.endswith("…"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
