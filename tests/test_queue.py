"""queue.py 单测：睡眠队列的入队、容量、过期、补发顺序与定时判定。"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "life"))

from core import queue  # noqa: E402
from core.gate import MessageKind  # noqa: E402
from core.queue import QueuedMessage, SleepQueue  # noqa: E402

G1 = "aiocqhttp:GroupMessage:100"
G2 = "aiocqhttp:GroupMessage:200"
P1 = "aiocqhttp:FriendMessage:300"


def qmsg(umo: str, ts: float, text: str = "在吗", sender: str = "2001", kind=MessageKind.ADDRESSED):
    return QueuedMessage(
        umo=umo, sender_id=sender, sender_name=sender, text=text, timestamp=ts, kind=kind
    )


class TestSleepQueue(unittest.TestCase):
    def test_push_and_drain_is_time_ordered(self):
        q = SleepQueue()
        q.push(qmsg(G1, ts=300, text="第三条"))
        q.push(qmsg(G2, ts=100, text="第一条"))
        q.push(qmsg(P1, ts=200, text="第二条"))
        drained = q.drain()
        self.assertEqual([m.text for m in drained], ["第一条", "第二条", "第三条"])
        self.assertEqual(len(q), 0)

    def test_per_session_order_preserved(self):
        q = SleepQueue()
        for i in range(3):
            q.push(qmsg(G1, ts=100 + i, text=f"m{i}"))
        self.assertEqual([m.text for m in q.drain_session(G1)], ["m0", "m1", "m2"])

    def test_cap_keeps_newest_and_reports_eviction(self):
        q = SleepQueue(max_per_session=2)
        q.push(qmsg(G1, ts=1, text="最旧"))
        q.push(qmsg(G1, ts=2, text="中间"))
        result = q.push(qmsg(G1, ts=3, text="最新"))
        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.evicted)
        self.assertEqual(result.evicted.text, "最旧")
        self.assertEqual([m.text for m in q.peek(G1)], ["中间", "最新"])

    def test_cap_is_per_session(self):
        q = SleepQueue(max_per_session=1)
        q.push(qmsg(G1, ts=1, text="g1"))
        q.push(qmsg(G2, ts=2, text="g2"))
        self.assertEqual(len(q), 2)

    def test_prune_drops_stale_messages(self):
        q = SleepQueue(max_age_hours=1.0)
        q.push(qmsg(G1, ts=0, text="很旧"))
        q.push(qmsg(G1, ts=3600 * 2, text="新"))
        dropped = q.prune(now=3600 * 2)
        self.assertEqual([m.text for m in dropped], ["很旧"])
        self.assertEqual([m.text for m in q.peek(G1)], ["新"])

    def test_drain_prunes_first(self):
        q = SleepQueue(max_age_hours=1.0)
        q.push(qmsg(G1, ts=0, text="很旧"))
        q.push(qmsg(G1, ts=7200, text="新"))
        self.assertEqual([m.text for m in q.drain(now=7200)], ["新"])

    def test_drain_session_leaves_others(self):
        q = SleepQueue()
        q.push(qmsg(G1, ts=1, text="g1"))
        q.push(qmsg(G2, ts=1, text="g2"))
        self.assertEqual(len(q.drain_session(G1)), 1)
        self.assertEqual([m.text for m in q.peek()], ["g2"])

    def test_empty_message_rejected(self):
        q = SleepQueue()
        result = q.push(QueuedMessage(umo=G1, sender_id="", sender_name="", text="", timestamp=1))
        self.assertFalse(result.accepted)
        self.assertEqual(len(q), 0)

    def test_counts_and_truthiness(self):
        q = SleepQueue()
        self.assertFalse(bool(q))
        q.push(qmsg(G1, ts=1))
        q.push(qmsg(G1, ts=2))
        q.push(qmsg(G2, ts=3))
        self.assertEqual(q.counts(), {G1: 2, G2: 1})
        self.assertTrue(bool(q))

    def test_describe_is_human_readable(self):
        line = qmsg(G1, ts=0, text="睡了吗").describe()
        self.assertIn("睡了吗", line)
        self.assertIn("2001", line)


class TestFlushTiming(unittest.TestCase):
    def test_next_flush_today_when_before_hour(self):
        self.assertEqual(
            queue.next_flush_time(datetime(2026, 10, 7, 7, 30), 8),
            datetime(2026, 10, 7, 8, 0),
        )

    def test_next_flush_tomorrow_when_already_past(self):
        self.assertEqual(
            queue.next_flush_time(datetime(2026, 10, 7, 8, 0), 8),
            datetime(2026, 10, 8, 8, 0),
        )
        self.assertEqual(
            queue.next_flush_time(datetime(2026, 10, 7, 9, 15), 8),
            datetime(2026, 10, 8, 8, 0),
        )

    def test_due_for_flush(self):
        self.assertFalse(queue.due_for_flush(datetime(2026, 10, 7, 7, 59), 8, None))
        self.assertTrue(queue.due_for_flush(datetime(2026, 10, 7, 8, 0), 8, None))
        self.assertFalse(
            queue.due_for_flush(datetime(2026, 10, 7, 8, 0), 8, date(2026, 10, 7))
        )
        self.assertTrue(queue.due_for_flush(datetime(2026, 10, 7, 10, 0), 8, None))
        self.assertFalse(
            queue.due_for_flush(datetime(2026, 10, 7, 10, 0), 8, date(2026, 10, 7))
        )
        # 跨天：昨天补发过，今天照样要补
        self.assertTrue(
            queue.due_for_flush(datetime(2026, 10, 8, 8, 0), 8, date(2026, 10, 7))
        )


class TestFlushText(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(queue.compose_flush_text([]), "")

    def test_single_message(self):
        text = queue.compose_flush_text([qmsg(G1, ts=1, text="睡了吗")])
        self.assertIn("刚看到", text)
        self.assertIn("2001：睡了吗", text)

    def test_quotes_in_time_order(self):
        text = queue.compose_flush_text(
            [qmsg(G1, ts=200, text="第二条", sender="小红"), qmsg(G1, ts=100, text="第一条", sender="小明")]
        )
        self.assertLess(text.index("第一条"), text.index("第二条"))

    def test_truncates_with_hint(self):
        messages = [qmsg(G1, ts=i, text=f"m{i}", sender=f"u{i}") for i in range(5)]
        text = queue.compose_flush_text(messages, max_quote=3)
        self.assertIn("m0", text)
        self.assertIn("m2", text)
        self.assertNotIn("m3", text)
        self.assertIn("还有 2 条", text)

    def test_multiline_text_collapsed(self):
        text = queue.compose_flush_text([qmsg(G1, ts=1, text="第一行\n第二行")])
        self.assertIn("2001：第一行 第二行", text)

    def test_missing_name_falls_back_to_id(self):
        message = QueuedMessage(umo=G1, sender_id="2001", sender_name="", text="hi", timestamp=1)
        self.assertIn("2001：hi", queue.compose_flush_text([message]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
