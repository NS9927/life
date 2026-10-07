"""gate.py 单测：判定链、概率、连续丢弃、循环熔断、会话冷却。

设计文档第二节、第八节的每条语义都对应这里的一个测试。
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot_plugin_reply_gate"))

from core import gate  # noqa: E402
from core.gate import Action, Gate, GateConfig, MessageInfo, MessageKind  # noqa: E402


class SpyRandom(random.Random):
    """固定返回值的 Random，顺便数一下到底掷了几次骰子。"""

    def __init__(self, value: float = 0.5):
        super().__init__()
        self.value = value
        self.calls = 0

    def random(self) -> float:  # type: ignore[override]
        self.calls += 1
        return self.value


def info(
    kind: MessageKind = MessageKind.CHIME,
    *,
    sender: str = "2001",
    umo: str = "aiocqhttp:GroupMessage:100",
    text: str = "在吗",
    ts: float = 0.0,
) -> MessageInfo:
    return MessageInfo(umo=umo, sender_id=sender, kind=kind, sender_name=sender, text=text, timestamp=ts)


def make_gate(**overrides) -> tuple[Gate, SpyRandom, list[float]]:
    now = [1000.0]
    rng = SpyRandom(overrides.pop("rng_value", 0.5))
    config = overrides.pop("config", None) or GateConfig(
        consecutive_drop_limit=overrides.pop("consecutive_drop_limit", 0),
        base_probability={"chime": 0.1, "proactive": 0.3, "addressed": 1.0},
        **overrides,
    )
    g = Gate(config, rng=rng, clock=lambda: now[0])
    return g, rng, now


class TestProbability(unittest.TestCase):
    def test_addressed_never_rolls_dice(self):
        # 被 @ / 私聊：哪怕活跃度低到 0.01，也绝不掷骰子
        g, rng, _ = make_gate(rng_value=0.999)
        decision = g.evaluate(info(MessageKind.ADDRESSED), activity=0.01, sleeping=False)
        self.assertIs(decision.action, Action.ALLOW)
        self.assertEqual(rng.calls, 0)
        self.assertEqual(decision.probability, 1.0)

    def test_chime_probability_is_base_times_activity(self):
        g, _, _ = make_gate(rng_value=0.04)  # 严格小于 0.05 才会放行
        decision = g.evaluate(info(), activity=0.5, sleeping=False)
        self.assertAlmostEqual(decision.probability, 0.05)
        self.assertIs(decision.action, Action.ALLOW)

    def test_roll_is_strictly_less_than_probability(self):
        # 边界：掷出的值正好等于概率时算没过（和内置 random.random() < p 的语义一致）
        g, _, _ = make_gate(rng_value=0.05)
        decision = g.evaluate(info(), activity=0.5, sleeping=False)
        self.assertIs(decision.action, Action.DROP)

    def test_chime_dropped_when_roll_fails(self):
        g, rng, _ = make_gate(rng_value=0.9)
        decision = g.evaluate(info(), activity=0.5, sleeping=False)
        self.assertIs(decision.action, Action.DROP)
        self.assertEqual(decision.reason, gate.REASON_PROBABILITY)
        self.assertEqual(rng.calls, 1)

    def test_proactive_uses_its_own_base(self):
        g, _, _ = make_gate(rng_value=0.5)
        decision = g.evaluate(info(MessageKind.PROACTIVE), activity=1.0, sleeping=False)
        self.assertAlmostEqual(decision.probability, 0.3)

    def test_activity_ceiling_prevents_over_one(self):
        g, _, _ = make_gate(rng_value=0.5)
        decision = g.evaluate(info(), activity=5.0, sleeping=False)  # 外部传脏了也不炸
        self.assertLessEqual(decision.probability, 1.0)


class TestSilenceList(unittest.TestCase):
    def test_silenced_sender_never_gets_through(self):
        for kind in (MessageKind.CHIME, MessageKind.ADDRESSED, MessageKind.PROACTIVE):
            g, rng, _ = make_gate(silence_list=frozenset({"2001"}))
            decision = g.evaluate(info(kind), activity=1.0, sleeping=False)
            self.assertIs(decision.action, Action.DROP, msg=kind)
            self.assertEqual(decision.reason, gate.REASON_SILENCE_LIST)
            self.assertEqual(rng.calls, 0)

    def test_other_senders_unaffected(self):
        g, _, _ = make_gate(rng_value=0.0, silence_list=frozenset({"9999"}))
        decision = g.evaluate(info(sender="2001"), activity=1.0, sleeping=False)
        self.assertIs(decision.action, Action.ALLOW)


class TestSleep(unittest.TestCase):
    def test_addressed_queued_during_sleep(self):
        g, _, _ = make_gate()
        decision = g.evaluate(info(MessageKind.ADDRESSED), activity=0.0, sleeping=True)
        self.assertIs(decision.action, Action.QUEUE)
        self.assertEqual(decision.reason, gate.REASON_SLEEP_QUEUED)

    def test_chime_dropped_during_sleep(self):
        g, rng, _ = make_gate()
        decision = g.evaluate(info(), activity=0.0, sleeping=True)
        self.assertIs(decision.action, Action.DROP)
        self.assertEqual(decision.reason, gate.REASON_SLEEP_CHIME)
        self.assertEqual(rng.calls, 0)

    def test_queue_only_addressed_can_be_disabled(self):
        g, _, _ = make_gate(sleep_queue_only_addressed=False)
        decision = g.evaluate(info(), activity=0.0, sleeping=True)
        self.assertIs(decision.action, Action.QUEUE)

    def test_queue_disabled_drops_addressed(self):
        g, _, _ = make_gate(sleep_queue_enable=False)
        decision = g.evaluate(info(MessageKind.ADDRESSED), activity=0.0, sleeping=True)
        self.assertIs(decision.action, Action.DROP)
        self.assertEqual(decision.reason, gate.REASON_SLEEP_DROPPED)

    def test_queue_resets_consecutive_drops(self):
        g, rng, _ = make_gate(rng_value=0.99)
        g.evaluate(info(), activity=0.5, sleeping=False)
        self.assertEqual(g.session("aiocqhttp:GroupMessage:100").consecutive_drops, 1)
        g.evaluate(info(MessageKind.ADDRESSED), activity=0.0, sleeping=True)
        self.assertEqual(g.session("aiocqhttp:GroupMessage:100").consecutive_drops, 0)


class TestConsecutiveDropFloor(unittest.TestCase):
    def test_forces_reply_after_limit(self):
        g, _, _ = make_gate(rng_value=0.99, consecutive_drop_limit=2)
        first = g.evaluate(info(), activity=0.5, sleeping=False)
        second = g.evaluate(info(), activity=0.5, sleeping=False)
        third = g.evaluate(info(), activity=0.5, sleeping=False)
        self.assertIs(first.action, Action.DROP)
        self.assertIs(second.action, Action.DROP)
        self.assertIs(third.action, Action.ALLOW)
        self.assertEqual(third.reason, gate.REASON_CONSECUTIVE_FLOOR)

    def test_allow_resets_counter(self):
        g, _, _ = make_gate(rng_value=0.0, consecutive_drop_limit=2)
        g.evaluate(info(), activity=0.5, sleeping=False)
        self.assertEqual(g.session("aiocqhttp:GroupMessage:100").consecutive_drops, 0)

    def test_disabled_when_zero(self):
        g, _, _ = make_gate(rng_value=0.99, consecutive_drop_limit=0)
        for _ in range(5):
            self.assertIs(g.evaluate(info(), activity=0.5, sleeping=False).action, Action.DROP)

    def test_floor_is_per_sender_by_default(self):
        # 真机教训：按会话计数会让热闹的群变成「每 N 条必插一句」——
        # 8 个人各说一句、彼此无关，会话计数照样累加。默认必须按发送者。
        g, _, _ = make_gate(rng_value=0.99, consecutive_drop_limit=2)
        for sender in ("A", "B", "C", "D", "E", "F"):
            decision = g.evaluate(info(sender=sender), activity=0.5, sleeping=False)
            self.assertIs(decision.action, Action.DROP, msg=f"{sender} 不该被强制放行")
        self.assertEqual(g.session("aiocqhttp:GroupMessage:100").consecutive_drops, 6)

    def test_same_sender_trips_the_floor(self):
        g, _, _ = make_gate(rng_value=0.99, consecutive_drop_limit=2)
        g.evaluate(info(sender="A"), activity=0.5, sleeping=False)
        g.evaluate(info(sender="A"), activity=0.5, sleeping=False)
        g.evaluate(info(sender="B"), activity=0.5, sleeping=False)  # B 只丢 1 条，不受影响
        third = g.evaluate(info(sender="A"), activity=0.5, sleeping=False)
        self.assertIs(third.action, Action.ALLOW)
        self.assertEqual(third.reason, gate.REASON_CONSECUTIVE_FLOOR)
        self.assertIn("该发送者", third.detail)

    def test_answering_clears_that_sender_only(self):
        g, _, _ = make_gate(rng_value=0.99, consecutive_drop_limit=2)
        g.evaluate(info(sender="A"), activity=0.5, sleeping=False)
        g.evaluate(info(sender="A"), activity=0.5, sleeping=False)
        g.evaluate(info(sender="B"), activity=0.5, sleeping=False)
        g.evaluate(info(sender="A"), activity=0.5, sleeping=False)  # A 被强制放行
        state = g.session("aiocqhttp:GroupMessage:100")
        self.assertEqual(state.dropped_from("A"), 0)
        self.assertEqual(state.dropped_from("B"), 1)

    def test_session_scope_keeps_old_behaviour(self):
        g, _, _ = make_gate(
            rng_value=0.99, consecutive_drop_limit=2, consecutive_drop_scope="session"
        )
        g.evaluate(info(sender="A"), activity=0.5, sleeping=False)
        g.evaluate(info(sender="B"), activity=0.5, sleeping=False)
        third = g.evaluate(info(sender="C"), activity=0.5, sleeping=False)
        self.assertIs(third.action, Action.ALLOW)
        self.assertIn("本会话", third.detail)

    def test_scope_config_parsing(self):
        self.assertEqual(GateConfig.from_raw({}).consecutive_drop_scope, "sender")
        self.assertEqual(
            GateConfig.from_raw({"consecutive_drop_scope": "SESSION"}).consecutive_drop_scope,
            "session",
        )
        self.assertEqual(
            GateConfig.from_raw({"consecutive_drop_scope": "垃圾"}).consecutive_drop_scope,
            "sender",
        )


class TestLoopBreaker(unittest.TestCase):
    def _alternate(self, g: Gate, rounds: int = 2) -> None:
        for _ in range(rounds):
            g.evaluate(info(sender="A"), activity=0.5, sleeping=False)
            g.evaluate(info(sender="B"), activity=0.5, sleeping=False)

    def test_alternation_triggers_mute(self):
        g, _, now = make_gate(rng_value=0.0, alternate_rounds=2, silence_minutes=30)
        self._alternate(g, rounds=2)
        state = g.session("aiocqhttp:GroupMessage:100")
        self.assertAlmostEqual(state.muted_until, now[0] + 30 * 60)

    def test_muted_session_is_dropped(self):
        g, _, _ = make_gate(rng_value=0.0, alternate_rounds=2)
        self._alternate(g, rounds=2)
        decision = g.evaluate(info(sender="A"), activity=1.0, sleeping=False)
        self.assertIs(decision.action, Action.DROP)
        self.assertEqual(decision.reason, gate.REASON_LOOP_BREAKER)
        self.assertIn("交替", decision.detail)

    def test_addressed_is_exempt_from_mute(self):
        g, _, _ = make_gate(rng_value=0.99, alternate_rounds=2)
        self._alternate(g, rounds=2)
        decision = g.evaluate(info(MessageKind.ADDRESSED, sender="A"), activity=0.5, sleeping=False)
        self.assertIs(decision.action, Action.ALLOW)

    def test_exemption_can_be_disabled(self):
        g, _, _ = make_gate(rng_value=0.99, alternate_rounds=2, exempt_addressed_from_loop=False)
        self._alternate(g, rounds=2)
        decision = g.evaluate(info(MessageKind.ADDRESSED, sender="A"), activity=0.5, sleeping=False)
        self.assertIs(decision.action, Action.DROP)
        self.assertEqual(decision.reason, gate.REASON_LOOP_BREAKER)

    def test_single_sender_never_triggers(self):
        g, _, _ = make_gate(rng_value=0.0, alternate_rounds=2)
        for _ in range(10):
            g.evaluate(info(sender="A"), activity=0.5, sleeping=False)
        self.assertEqual(g.session("aiocqhttp:GroupMessage:100").muted_until, 0.0)

    def test_third_party_breaks_alternation(self):
        g, _, _ = make_gate(rng_value=0.0, alternate_rounds=2)
        for seq in ("A", "B", "A", "C", "A", "B", "A", "C"):
            g.evaluate(info(sender=seq), activity=0.5, sleeping=False)
        self.assertEqual(g.session("aiocqhttp:GroupMessage:100").muted_until, 0.0)

    def test_mute_expires(self):
        g, _, now = make_gate(rng_value=0.0, alternate_rounds=2, silence_minutes=1)
        self._alternate(g, rounds=2)
        now[0] += 61
        decision = g.evaluate(info(sender="A"), activity=1.0, sleeping=False)
        self.assertNotEqual(decision.reason, gate.REASON_LOOP_BREAKER)

    def test_disabled_never_mutes(self):
        g, _, _ = make_gate(rng_value=0.0, alternate_rounds=2, loop_breaker_enable=False)
        self._alternate(g, rounds=5)
        self.assertEqual(g.session("aiocqhttp:GroupMessage:100").muted_until, 0.0)


class TestCooldown(unittest.TestCase):
    def test_drops_after_max_replies_in_window(self):
        g, _, now = make_gate(rng_value=0.0, cooldown_max_replies=2, cooldown_window_seconds=60)
        g.record_reply("aiocqhttp:GroupMessage:100")
        g.record_reply("aiocqhttp:GroupMessage:100")
        now[0] += 10
        decision = g.evaluate(info(), activity=1.0, sleeping=False)
        self.assertIs(decision.action, Action.DROP)
        self.assertEqual(decision.reason, gate.REASON_COOLDOWN)

    def test_addressed_exempt(self):
        g, _, now = make_gate(rng_value=0.99, cooldown_max_replies=1, cooldown_window_seconds=60)
        g.record_reply("aiocqhttp:GroupMessage:100")
        now[0] += 5
        decision = g.evaluate(info(MessageKind.ADDRESSED), activity=1.0, sleeping=False)
        self.assertIs(decision.action, Action.ALLOW)

    def test_exemption_can_be_disabled(self):
        g, _, now = make_gate(
            rng_value=0.99,
            cooldown_max_replies=1,
            cooldown_window_seconds=60,
            exempt_addressed_from_cooldown=False,
        )
        g.record_reply("aiocqhttp:GroupMessage:100")
        now[0] += 5
        decision = g.evaluate(info(MessageKind.ADDRESSED), activity=1.0, sleeping=False)
        self.assertEqual(decision.reason, gate.REASON_COOLDOWN)

    def test_window_slides(self):
        g, _, now = make_gate(rng_value=0.0, cooldown_max_replies=1, cooldown_window_seconds=60)
        g.record_reply("aiocqhttp:GroupMessage:100")
        now[0] += 61
        decision = g.evaluate(info(), activity=1.0, sleeping=False)
        self.assertIs(decision.action, Action.ALLOW)

    def test_cooldown_is_per_session(self):
        g, _, now = make_gate(rng_value=0.0, cooldown_max_replies=1, cooldown_window_seconds=60)
        g.record_reply("aiocqhttp:GroupMessage:100")
        now[0] += 5
        decision = g.evaluate(info(umo="aiocqhttp:GroupMessage:200"), activity=1.0, sleeping=False)
        self.assertIs(decision.action, Action.ALLOW)


class TestConfigFromRaw(unittest.TestCase):
    def test_defaults_when_empty(self):
        cfg = GateConfig.from_raw(None)
        self.assertEqual(cfg.consecutive_drop_limit, 3)
        self.assertEqual(cfg.base_probability["chime"], 0.03)
        self.assertTrue(cfg.loop_breaker_enable)
        self.assertEqual(cfg.silence_list, frozenset())

    def test_reads_schema_shape(self):
        raw = {
            "base_probability": {"chime": 0.2, "proactive": 0.4, "addressed": 1.0},
            "consecutive_drop_limit": 3,
            "silence_list": ["10001", " 10002 ", ""],
            "loop_breaker": {"enable": False, "alternate_rounds": 6, "silence_minutes": 15},
            "session_cooldown": {"enable": False, "window_seconds": 30, "max_replies": 5},
            "sleep_queue": {"enable": True, "only_addressed": False},
        }
        cfg = GateConfig.from_raw(raw)
        self.assertEqual(cfg.base_probability["chime"], 0.2)
        self.assertEqual(cfg.consecutive_drop_limit, 3)
        self.assertEqual(cfg.silence_list, frozenset({"10001", "10002"}))
        self.assertFalse(cfg.loop_breaker_enable)
        self.assertEqual(cfg.alternate_rounds, 6)
        self.assertEqual(cfg.silence_minutes, 15.0)
        self.assertFalse(cfg.cooldown_enable)
        self.assertEqual(cfg.cooldown_max_replies, 5)
        self.assertFalse(cfg.sleep_queue_only_addressed)

    def test_silence_list_accepts_text(self):
        cfg = GateConfig.from_raw({"silence_list": "10001\n10002,10003"})
        self.assertEqual(cfg.silence_list, frozenset({"10001", "10002", "10003"}))

    def test_broken_values_do_not_crash(self):
        cfg = GateConfig.from_raw({"base_probability": "不是字典", "consecutive_drop_limit": "x"})
        self.assertEqual(cfg.base_probability["chime"], 0.03)


class TestDecisionShape(unittest.TestCase):
    def test_log_line_contains_key_facts(self):
        g, _, _ = make_gate(rng_value=0.99)
        decision = g.evaluate(info(), activity=0.5, sleeping=False)
        line = decision.log_line()
        self.assertIn("drop", line)
        self.assertIn("probability", line)
        self.assertIn("活跃度=0.50", line)

    def test_allowed_property(self):
        g, _, _ = make_gate(rng_value=0.0)
        self.assertTrue(g.evaluate(info(), activity=1.0, sleeping=False).allowed)
        g2, _, _ = make_gate(rng_value=0.99)
        self.assertFalse(g2.evaluate(info(), activity=0.5, sleeping=False).allowed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
