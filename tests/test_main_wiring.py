"""main.py 接线集成测试：用桩掉的 astrbot 真跑一遍钩子。

验的是**适配层**，不是 AstrBot：
- 钩子注册的调用姿势对不对（priority / 装饰器）
- 事件标志位到消息语义的映射对不对（被 @ 绝不用概率）
- stop_event 有没有在对的时机调
- active_reply 概率有没有被写、写的是不是内存对象
- 队列、补发、冷却计时有没有接上

运行方式不变：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "life"))

from tests._astrbot_stub import FakePlain, install  # noqa: E402

LOGGER = install()  # 必须在 import main 之前

from life import main as plugin_main  # noqa: E402
from life.core.gate import Action  # noqa: E402
from life.core.queue import QueuedMessage  # noqa: E402

UMO = "aiocqhttp:GroupMessage:100"


class _MsgObj:
    """只为 message_obj.timestamp 存在的假对象。"""

    def __init__(self, timestamp: float = 0.0) -> None:
        self.timestamp = timestamp


class FakeEvent:
    """够用的假事件：只实现 main.py 真正调用的方法。"""

    def __init__(
        self,
        *,
        umo: str = UMO,
        sender: str = "2001",
        self_id: str = "9999",
        private: bool = False,
        at_or_wake: bool = False,
        text: str = "在吗",
        sender_name: str = "某人",
        with_result: bool = True,
        message_ts: float = 0.0,
    ) -> None:
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = at_or_wake
        self._sender = sender
        self._self_id = self_id
        self._private = private
        self.message_str = text
        self._name = sender_name
        self.message_obj = _MsgObj(message_ts)
        self._stopped = False
        self._result = object() if with_result else None

    def get_sender_id(self) -> str:
        return self._sender

    def get_self_id(self) -> str:
        return self._self_id

    def is_private_chat(self) -> bool:
        return self._private

    def get_sender_name(self) -> str:
        return self._name

    def get_message_str(self) -> str:
        return self.message_str

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped

    def get_result(self):
        return None if self._stopped else self._result


class FakeContext:
    def __init__(self, active_reply: dict | None = None) -> None:
        self.sent: list[tuple[str, object]] = []
        self.registered_web_apis: list[tuple] = []
        self.active_reply = (
            active_reply
            if active_reply is not None
            else {"enable": True, "method": "possibility_reply", "possibility_reply": 0.1}
        )
        self.config_obj = {"provider_ltm_settings": {"active_reply": self.active_reply}}

    def register_web_api(self, route, view_handler, methods, desc) -> None:
        self.registered_web_apis = [r for r in self.registered_web_apis if r[0] != route]
        self.registered_web_apis.append((route, view_handler, methods, desc))

    def get_config(self, umo: str | None = None) -> dict:
        return self.config_obj

    async def send_message(self, session: str, message_chain) -> bool:
        self.sent.append((session, message_chain))
        return True


def make_config(**over) -> dict:
    cfg = {
        "enable": True,
        "time_weights": "0-24=100",  # 全天活跃度 1.0：基础概率直接等于放行概率，便于确定断言
        "base_probability": {"chime": 0.0, "proactive": 0.3, "addressed": 1.0},
        "consecutive_drop_limit": 0,
        "loop_breaker": {"enable": False},
        "session_cooldown": {"enable": False},
        "sleep_queue": {"enable": True, "flush_at_hour": 8, "only_addressed": True},
        "reply_delay": {"enable": False},
        # 默认关掉分档（阈值 0 = 永远立刻回），单独测分档时再覆盖
        "addressed_reply": {"immediate_threshold": 0.0, "batch_window_seconds": 0.05},
        "silence_list": [],
        "session_whitelist": [],
        "verbose_log": True,
    }
    cfg.update(over)
    return cfg


class WiringTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        LOGGER.records.clear()  # 每个测试只看自己产生的日志

    async def asyncTearDown(self) -> None:
        plugin = getattr(self, "plugin", None)
        if plugin is not None:
            await plugin.terminate()

    def build(self, active_reply: dict | None = None, **over) -> "plugin_main.ReplyGate":
        self.ctx = FakeContext(active_reply=active_reply)
        self.plugin = plugin_main.ReplyGate(self.ctx, config=make_config(**over))
        return self.plugin


class TestChimePath(WiringTestBase):
    async def test_allowed_chime_opens_builtin_probability(self):
        plugin = self.build(**{"base_probability": {"chime": 1.0, "proactive": 0.3, "addressed": 1.0}})
        event = FakeEvent()
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())
        # 内置那只手必须被顶到 1.0，否则会被二次掷骰子（概率被平方）
        self.assertEqual(self.ctx.active_reply["possibility_reply"], 1.0)

    async def test_dropped_chime_stops_event_and_leaves_config_alone(self):
        plugin = self.build()  # chime 概率 0.0 → 必丢
        event = FakeEvent()
        await plugin.gate(event)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.ctx.active_reply["possibility_reply"], 0.1)
        self.assertTrue(any("已读不回" in line for line in LOGGER.messages()))

    async def test_activity_scales_chime_probability(self):
        # 全天权重 1 → 活跃度 0.01；基础 0.1 → 概率 0.001，几乎必丢
        plugin = self.build(
            time_weights="0-24=1",
            base_probability={"chime": 0.1, "proactive": 0.3, "addressed": 1.0},
        )
        drops = 0
        for _ in range(30):
            event = FakeEvent()
            await plugin.gate(event)
            drops += int(event.is_stopped())
        self.assertGreaterEqual(drops, 25)

    async def test_silence_list_drops_chime(self):
        plugin = self.build(silence_list=["2001"])
        event = FakeEvent()
        await plugin.gate(event)
        self.assertTrue(event.is_stopped())


class TestAddressedPath(WiringTestBase):
    async def test_at_bot_survives_tiny_activity(self):
        plugin = self.build(time_weights="0-24=1")  # 活跃度 0.01
        event = FakeEvent(at_or_wake=True)
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())

    async def test_private_chat_counts_as_addressed(self):
        # 有的配置下私聊不会被置 is_at_or_wake_command，必须靠 is_private_chat 兜住
        plugin = self.build(time_weights="0-24=1")
        event = FakeEvent(private=True, at_or_wake=False, umo="aiocqhttp:FriendMessage:2001")
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())

    async def test_silence_list_beats_addressing(self):
        plugin = self.build(silence_list=["2001"])
        event = FakeEvent(at_or_wake=True)
        await plugin.gate(event)
        self.assertTrue(event.is_stopped())

    async def test_self_message_ignored(self):
        plugin = self.build()
        event = FakeEvent(sender="9999", self_id="9999")
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())
        self.assertEqual(self.ctx.active_reply["possibility_reply"], 0.1)


class TestSleepPath(WiringTestBase):
    async def test_addressed_message_queued_and_stopped(self):
        plugin = self.build(time_weights="0-24=0")  # 权重 0 = 睡眠
        event = FakeEvent(at_or_wake=True, text="睡了吗")
        await plugin.gate(event)
        self.assertTrue(event.is_stopped())  # 现在不花 token
        self.assertEqual(len(plugin._queue), 1)  # 但排队了
        queued = plugin._queue.peek()[0]
        self.assertEqual(queued.text, "睡了吗")
        self.assertEqual(queued.umo, UMO)

    async def test_group_chime_dropped_not_queued(self):
        plugin = self.build(time_weights="0-24=0")
        event = FakeEvent(at_or_wake=False)
        await plugin.gate(event)
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(plugin._queue), 0)

    async def test_private_message_queued_during_sleep(self):
        plugin = self.build(time_weights="0-24=0")
        event = FakeEvent(private=True, umo="aiocqhttp:FriendMessage:2001")
        await plugin.gate(event)
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(plugin._queue), 1)


class TestFlush(WiringTestBase):
    async def test_flush_sends_queued_messages(self):
        plugin = self.build()
        plugin._flush_hour = 0  # 保证「已过整点」
        plugin._last_flush_date = None
        now = time.time()  # 必须用真实时间戳，否则会被 max_age_hours 清掉
        plugin._queue.push(
            QueuedMessage(umo=UMO, sender_id="2001", sender_name="小明", text="在吗", timestamp=now - 60)
        )
        plugin._queue.push(
            QueuedMessage(umo=UMO, sender_id="2002", sender_name="小红", text="睡了吗", timestamp=now - 30)
        )
        await plugin._maybe_flush()

        self.assertEqual(len(self.ctx.sent), 1)
        session, chain = self.ctx.sent[0]
        self.assertEqual(session, UMO)
        self.assertIsInstance(chain.chain[0], FakePlain)
        text = chain.chain[0].text
        self.assertIn("刚看到", text)
        self.assertIn("小明", text)
        self.assertIn("睡了吗", text)

    async def test_stale_messages_are_dropped_before_flush(self):
        plugin = self.build()
        plugin._flush_hour = 0
        plugin._last_flush_date = None
        plugin._queue.push(
            QueuedMessage(umo=UMO, sender_id="2001", sender_name="小明", text="昨天的", timestamp=1.0)
        )
        await plugin._maybe_flush()
        self.assertEqual(len(self.ctx.sent), 0)

    async def test_flush_happens_once_per_day(self):
        plugin = self.build()
        plugin._flush_hour = 0
        plugin._last_flush_date = None
        plugin._queue.push(
            QueuedMessage(
                umo=UMO,
                sender_id="2001",
                sender_name="小明",
                text="在吗",
                timestamp=time.time() - 10,
            )
        )
        await plugin._maybe_flush()
        await plugin._maybe_flush()
        self.assertEqual(len(self.ctx.sent), 1)

    async def test_flush_before_hour_waits(self):
        # flush_at_hour 会 %24，所以「还没到点」的那个整点得动态算
        not_due_hour = (datetime.now().hour + 1) % 24
        if not_due_hour == 0:
            self.skipTest("当前 23 点，今天已没有『还没到』的整点")
        plugin = self.build()
        plugin._flush_hour = not_due_hour
        plugin._last_flush_date = None
        plugin._queue.push(
            QueuedMessage(
                umo=UMO,
                sender_id="2001",
                sender_name="小明",
                text="在吗",
                timestamp=time.time() - 10,
            )
        )
        await plugin._maybe_flush()
        self.assertEqual(len(self.ctx.sent), 0)
        self.assertEqual(len(plugin._queue), 1)


class TestCooldownWiring(WiringTestBase):
    async def test_reply_is_recorded_even_when_delay_disabled(self):
        plugin = self.build(reply_delay={"enable": False})
        event = FakeEvent()
        await plugin.apply_reply_delay(event)
        self.assertEqual(len(plugin._gate.session(UMO).reply_times), 1)

    async def test_cooldown_then_drops_chime(self):
        plugin = self.build(
            base_probability={"chime": 1.0, "proactive": 0.3, "addressed": 1.0},
            session_cooldown={"enable": True, "window_seconds": 60, "max_replies": 1},
            consecutive_drop_limit=0,
        )
        await plugin.apply_reply_delay(FakeEvent())  # 记一次「刚回过」
        event = FakeEvent()
        await plugin.gate(event)
        self.assertTrue(event.is_stopped())
        self.assertTrue(any("cooldown" in line for line in LOGGER.messages()))

    async def test_stopped_event_is_not_recorded(self):
        plugin = self.build()
        event = FakeEvent()
        event.stop_event()
        await plugin.apply_reply_delay(event)
        self.assertEqual(len(plugin._gate.session(UMO).reply_times), 0)


class TestAddressedTiers(WiringTestBase):
    """活跃度分档：高→立刻回；低→攒一波统一回；0→睡眠排队（另一组测试）。"""

    def batch_cfg(self, window: float = 0.2):
        return {
            "enable": True,
            "immediate_threshold": 0.6,
            "batch_window_seconds": window,
            "batch_max_messages": 10,
        }

    async def test_high_activity_replies_immediately(self):
        plugin = self.build(time_weights="0-24=100", addressed_reply=self.batch_cfg())
        event = FakeEvent(at_or_wake=True)
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())
        self.assertFalse(plugin._batch.active(UMO))

    async def test_low_activity_waits_then_replies_with_everything(self):
        plugin = self.build(time_weights="0-24=10", addressed_reply=self.batch_cfg(window=0.2))
        leader = FakeEvent(at_or_wake=True, text="第一条")
        task = asyncio.create_task(plugin.gate(leader))  # 领队在等窗口

        await asyncio.sleep(0.02)
        folded = FakeEvent(at_or_wake=True, text="第二条")
        await plugin.gate(folded)

        # 后到的那条被 stop（0 token），内容并进领队的正文
        self.assertTrue(folded.is_stopped())
        await task
        self.assertFalse(leader.is_stopped())
        self.assertIn("第一条", leader.message_str)
        self.assertIn("第二条", leader.message_str)
        self.assertTrue(any("统一回复" in line for line in LOGGER.messages()))

    async def test_two_sessions_do_not_share_a_batch(self):
        plugin = self.build(time_weights="0-24=10", addressed_reply=self.batch_cfg(window=0.05))
        other = FakeEvent(at_or_wake=True, umo="aiocqhttp:GroupMessage:200")
        await plugin.gate(other)
        self.assertEqual(len(plugin._batch.pending()), 0)  # 领队已结束并清空


class TestSafetyWiring(WiringTestBase):
    async def test_gate_exception_fails_open(self):
        plugin = self.build()

        async def boom(_event):
            raise RuntimeError("闸门内部炸了")

        plugin._decide = boom  # type: ignore[assignment]
        event = FakeEvent()
        await plugin.gate(event)  # 不许抛出去
        self.assertFalse(event.is_stopped())  # 也不许把消息吃掉
        self.assertTrue(any("fail-open" in line for line in LOGGER.messages()))

    async def test_whitelist_limits_scope(self):
        plugin = self.build(session_whitelist=["aiocqhttp:GroupMessage:999"])
        event = FakeEvent(umo=UMO, at_or_wake=True)
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())
        self.assertEqual(self.ctx.active_reply["possibility_reply"], 0.1)

    async def test_missing_active_reply_does_not_crash(self):
        plugin = self.build(**{"base_probability": {"chime": 1.0, "proactive": 0.3, "addressed": 1.0}})
        self.ctx.config_obj = {}  # 内置配置里压根没有这一节
        event = FakeEvent()
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())
        self.assertTrue(any("possibility_reply" in line for line in LOGGER.messages("warning")))

    async def test_disabled_plugin_does_nothing(self):
        plugin = self.build(enable=False)
        event = FakeEvent()
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())
        self.assertEqual(self.ctx.active_reply["possibility_reply"], 0.1)

    async def test_broken_time_weights_falls_back(self):
        plugin = self.build(time_weights="这不是作息表")
        self.assertTrue(len(plugin._segments) > 0)
        event = FakeEvent()
        await plugin.gate(event)  # 不该抛
        self.assertTrue(event.is_stopped())  # 回退到默认表（白天群聊概率低）+ chime 0.0

    async def test_initialize_and_terminate(self):
        plugin = self.build()
        await plugin.initialize()
        self.assertIsNotNone(plugin._task)
        await plugin.terminate()
        self.assertTrue(plugin._task is None)

    async def test_page_routes_are_registered(self):
        plugin = self.build()
        await plugin.initialize()
        routes = {r[0]: r[2] for r in self.ctx.registered_web_apis}
        self.assertIn("/life/page/config", routes)
        self.assertEqual(routes["/life/page/config"], ["GET"])
        self.assertIn("/life/page/curve", routes)
        self.assertIn("/life/page/preview", routes)
        self.assertIn("/life/page/settings", routes)
        self.assertEqual(routes["/life/page/settings"], ["POST"])

    async def test_stats_snapshot_counts_decisions(self):
        plugin = self.build(
            **{"base_probability": {"chime": 0.0, "proactive": 0.3, "addressed": 1.0}}
        )
        await plugin.gate(FakeEvent())  # 丢一条
        snapshot = plugin.stats_snapshot()
        self.assertEqual(snapshot["drops"], 1)
        self.assertEqual(snapshot["allows"], 0)


class TestStaleAddressedWiring(WiringTestBase):
    async def test_stale_addressed_is_stopped(self):
        plugin = self.build(addressed_max_age_seconds=180.0)
        event = FakeEvent(at_or_wake=True, message_ts=time.time() - 600)
        await plugin.gate(event)
        self.assertTrue(event.is_stopped())
        self.assertTrue(any("stale" in line for line in LOGGER.messages()))

    async def test_fresh_addressed_passes(self):
        plugin = self.build(addressed_max_age_seconds=180.0)
        event = FakeEvent(at_or_wake=True, message_ts=time.time() - 5)
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())

    async def test_platform_without_timestamp_is_not_punished(self):
        plugin = self.build(addressed_max_age_seconds=180.0)
        event = FakeEvent(at_or_wake=True)  # message_ts 默认 0
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())

    async def test_millisecond_timestamp_is_normalised(self):
        plugin = self.build(addressed_max_age_seconds=180.0)
        event = FakeEvent(at_or_wake=True, message_ts=(time.time() - 5) * 1000)  # 毫秒
        await plugin.gate(event)
        self.assertFalse(event.is_stopped())



if __name__ == "__main__":
    unittest.main(verbosity=2)
