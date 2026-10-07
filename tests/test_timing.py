"""延迟埋点测试：记录字段、超行截断、I/O 失败不影响调用方、main.py 接线。

分三层：
1. ``life.core.timing`` 纯逻辑（不依赖 AstrBot，不碰文件）；
2. ``life.main.TimingSink`` 的真实文件 I/O（临时目录，Python 标准库 tempfile）；
3. ``main.py`` 的接线（用 tests/_astrbot_stub.py 桩掉 astrbot 真跑一遍钩子）：
   闸门阶段挂记录 → 装饰阶段补齐 t_decorate/delay/t_ready 再落盘；DROP/QUEUE/攒批就地落盘。

核心约束：埋点**完全不改变判定行为**，任何 I/O 异常都不许抛给调用方。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "life"))

from tests._astrbot_stub import install  # noqa: E402

LOGGER = install()  # 必须在 import main 之前

from life import main as plugin_main  # noqa: E402
from life.core import timing  # noqa: E402

UMO = "aiocqhttp:GroupMessage:100"


# ----------------------------------------------------------------------
# 1. 纯逻辑
# ----------------------------------------------------------------------
class TestRecordShape(unittest.TestCase):
    def test_missing_fields_become_null(self):
        record = timing.normalize_record({"umo": UMO, "action": "drop"})
        self.assertEqual(set(record), set(timing.TIMING_FIELDS))
        for name in timing.TIMING_FIELDS:
            if name not in ("umo", "action"):
                self.assertIsNone(record[name], name)

    def test_encode_keeps_unicode_readable(self):
        line = timing.encode_record({"umo": UMO, "reason": "睡眠期群聊闲聊"})
        self.assertNotIn("\\u", line)  # ensure_ascii=False
        self.assertIn("睡眠期群聊闲聊", line)
        self.assertNotIn("\n", line)  # 一行就是一行
        self.assertEqual(json.loads(line)["umo"], UMO)

    def test_encode_is_single_line_json_with_all_fields(self):
        payload = json.loads(timing.encode_record({"t_gate": 1.5, "batch": None}))
        self.assertEqual(set(payload), set(timing.TIMING_FIELDS))
        self.assertEqual(payload["t_gate"], 1.5)

    def test_unknown_keys_are_dropped(self):
        payload = json.loads(timing.encode_record({"umo": UMO, "偷偷加的键": 1}))
        self.assertNotIn("偷偷加的键", payload)
        self.assertEqual(payload["umo"], UMO)

    def test_non_serializable_value_does_not_raise(self):
        payload = json.loads(timing.encode_record({"umo": object()}))
        self.assertIsInstance(payload["umo"], str)

    def test_empty_and_broken_input_does_not_raise(self):
        for broken in (None, {}, [], "不是 dict", 3):
            payload = json.loads(timing.encode_record(broken))
            self.assertEqual(set(payload), set(timing.TIMING_FIELDS))


class TestRotation(unittest.TestCase):
    def test_needs_rotate_by_lines_or_bytes(self):
        self.assertFalse(timing.needs_rotate(10, 1024))
        self.assertTrue(timing.needs_rotate(timing.MAX_LINES, 0))
        self.assertTrue(timing.needs_rotate(1, timing.MAX_BYTES))

    def test_trim_keeps_newest_half(self):
        lines = [f"line-{i}" for i in range(timing.MAX_LINES)]
        kept = timing.trim_lines(lines)
        self.assertEqual(len(kept), timing.MAX_LINES // 2)
        self.assertEqual(kept[-1], f"line-{timing.MAX_LINES - 1}")  # 保留最新的
        self.assertNotIn("line-0", kept)  # 最旧的被清掉

    def test_trim_respects_byte_cap(self):
        big = "x" * 4000
        kept = timing.trim_lines([big] * 1000)
        self.assertLess(sum(len(line.encode("utf-8")) + 1 for line in kept), timing.MAX_BYTES)
        self.assertGreaterEqual(len(kept), 1)  # 至少留一行，绝不返回空

    def test_trim_never_returns_empty(self):
        self.assertEqual(timing.trim_lines([]), [])

    def test_decode_dump_round_trip(self):
        lines = ['{"a":1}', '{"b":"中文"}']
        self.assertEqual(timing.decode_lines(timing.dump_lines(lines)), lines)
        self.assertEqual(timing.decode_lines("\n\n"), [])  # 空行丢掉

    def test_decode_tolerates_broken_tail(self):
        # 上次写一半断电：最后一行不是完整 JSON 也不许抛，交给上层 json 解析去容错
        self.assertEqual(timing.decode_lines('{"ok":1}\n{"bad"'), ['{"ok":1}', '{"bad"'])


class Reply:
    """照抄 AstrBot ``astrbot.core.message.components.Reply`` 的类名与字段。"""

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class TypedReply:
    """类名对不上、但有 ``type = "reply"`` 的鸭子类型组件。"""

    type = "reply"

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class _Plain:
    type = "plain"

    def __init__(self, text="hi"):
        self.text = text


class TestQuotedId(unittest.TestCase):
    def test_astrbot_reply_component(self):
        self.assertEqual(timing.quoted_id_from_message_chain([_Plain(), Reply(id="777")]), "777")

    def test_message_id_fallback(self):
        self.assertEqual(timing.quoted_id_from_message_chain([Reply(message_id="888")]), "888")

    def test_typed_reply_component(self):
        self.assertEqual(timing.quoted_id_from_message_chain([TypedReply(id="777")]), "777")

    def test_no_reply_component(self):
        self.assertEqual(timing.quoted_id_from_message_chain([_Plain(), _Plain()]), "")

    def test_reply_without_id(self):
        self.assertEqual(timing.quoted_id_from_message_chain([Reply(text="引用")]), "")

    def test_dict_style_component(self):
        self.assertEqual(timing.quoted_id_from_message_chain([{"type": "reply", "id": "999"}]), "999")

    def test_broken_chain_does_not_raise(self):
        self.assertEqual(timing.quoted_id_from_message_chain(None), "")
        self.assertEqual(timing.quoted_id_from_message_chain(12345), "")


# ----------------------------------------------------------------------
# 2. TimingSink 的真实文件 I/O
# ----------------------------------------------------------------------
class TestTimingSink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "plugin_data" / "life" / "timing.jsonl"
        self.sink = plugin_main.TimingSink(self.path)
        LOGGER.records.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def read_lines(self):
        return self.path.read_text(encoding="utf-8").splitlines()

    def test_write_creates_dirs_and_appends(self):
        self.assertTrue(self.sink.write({"umo": UMO, "action": "drop"}))
        self.assertTrue(self.sink.write({"umo": UMO, "action": "allow"}))
        lines = self.read_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["action"], "drop")
        self.assertEqual(json.loads(lines[1])["action"], "allow")

    def test_write_failure_is_swallowed_and_warned(self):
        def boom(_path, _line):
            raise OSError("磁盘满了")

        self.sink._append = boom  # monkeypatch：模拟 I/O 失败
        self.assertFalse(self.sink.write({"umo": UMO}))  # 不抛出去，只返回 False
        self.assertTrue(any("埋点写入失败" in msg for msg in LOGGER.messages("warning")))
        self.assertFalse(self.path.exists())

    def test_directory_creation_failure_is_swallowed(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("我是一个文件，不是目录", encoding="utf-8")
        sink = plugin_main.TimingSink(blocker / "timing.jsonl")  # 目录建不出来
        self.assertFalse(sink.write({"umo": UMO}))  # 依然不抛
        self.assertTrue(any("埋点写入失败" in msg for msg in LOGGER.messages("warning")))

    def test_counts_existing_lines_then_rotates(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "".join(f'{{"umo":"old-{i}"}}\n' for i in range(plugin_main.timing_mod.MAX_LINES)),
            encoding="utf-8",
        )
        self.assertTrue(self.sink.write({"umo": "newest"}))
        lines = self.read_lines()
        # 超 2000 行 → 清空重写保留最近一半，再追加这一条
        self.assertEqual(len(lines), plugin_main.timing_mod.MAX_LINES // 2 + 1)
        self.assertEqual(json.loads(lines[-1])["umo"], "newest")
        self.assertEqual(json.loads(lines[0])["umo"], "old-1000")  # 最旧的那些被清掉了

    def test_path_resolution_failure_is_swallowed(self):
        sink = plugin_main.TimingSink()  # 不注入路径 → 真机才会去解析 AstrBot 数据目录

        def boom():
            raise RuntimeError("AstrBot 没装 / 路径解析失败")

        sink.path = boom
        self.assertFalse(sink.write({"umo": UMO}))


# ----------------------------------------------------------------------
# 3. main.py 接线
# ----------------------------------------------------------------------
class _MsgObj:
    """message_obj：``timestamp`` = AstrBot 接收时间，``raw_message`` = 原始平台事件。"""

    def __init__(self, timestamp=0.0, message_id="", chain=None, raw_message=None):
        self.timestamp = timestamp
        self.message_id = message_id
        self.message = chain if chain is not None else []
        self.raw_message = raw_message


class FakeEvent:
    """够用的假事件：main.py 真正调用的方法 + AstrBot 的 extras 机制。"""

    def __init__(
        self,
        *,
        umo=UMO,
        sender="2001",
        self_id="9999",
        private=False,
        at_or_wake=False,
        text="在吗",
        with_result=True,
        message_ts=0.0,
        message_id="",
        chain=None,
        raw_event=None,
    ):
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = at_or_wake
        self._sender = sender
        self._self_id = self_id
        self._private = private
        self.message_str = text
        self.message_obj = _MsgObj(message_ts, message_id, chain, raw_event)
        self._stopped = False
        self._result = object() if with_result else None
        self._extras: dict = {}

    # -- AstrBot 事件 API --
    def get_sender_id(self):
        return self._sender

    def get_self_id(self):
        return self._self_id

    def is_private_chat(self):
        return self._private

    def get_sender_name(self):
        return "某人"

    def get_message_str(self):
        return self.message_str

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped

    def get_result(self):
        return None if self._stopped else self._result

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


class FakeContext:
    def __init__(self):
        self.config_obj = {"provider_ltm_settings": {"active_reply": {"possibility_reply": 0.1}}}

    def register_web_api(self, route, handler, methods, desc):
        pass

    def get_config(self, umo=None):
        return self.config_obj

    async def send_message(self, session, chain):
        return True


def make_config(**over):
    cfg = {
        "enable": True,
        "time_weights": "0-24=100",  # 全天活跃度 1.0
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
    cfg.update(over)
    return cfg


class WiringTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "timing.jsonl"
        LOGGER.records.clear()

    def tearDown(self):
        self.tmp.cleanup()

    async def asyncTearDown(self):
        plugin = getattr(self, "plugin", None)
        if plugin is not None:
            await plugin.terminate()

    def build(self, **over):
        self.plugin = plugin_main.ReplyGate(FakeContext(), config=make_config(**over))
        # 不写死到 AstrBot 数据目录：把 sink 指到临时文件
        self.plugin._timing_sink = plugin_main.TimingSink(self.path)
        return self.plugin

    def read(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]


class TestWiringOffByDefault(WiringTestBase):
    async def test_disabled_writes_nothing(self):
        plugin = self.build()  # 配置里没有 debug_timing → 默认关
        self.assertFalse(plugin._timing_enabled)
        dropped = FakeEvent()
        await plugin.gate(dropped)
        await plugin.apply_reply_delay(FakeEvent())
        self.assertEqual(self.read(), [])
        self.assertFalse(self.path.exists())  # 文件都不创建

    async def test_disabled_never_resolves_astrbot_path(self):
        plugin = self.build()
        sink = plugin._timing_sink

        def boom():
            raise RuntimeError("默认关闭时不该去碰 AstrBot 数据目录")

        sink.path = boom  # 真去解析就会炸
        await plugin.gate(FakeEvent())
        await plugin.apply_reply_delay(FakeEvent())
        self.assertIsNone(sink._resolved)  # 连解析都没发生过

    async def test_default_config_value_is_false(self):
        plugin = self.build(debug_timing=False)
        self.assertFalse(plugin._timing_enabled)
        self.assertFalse(plugin.stats_snapshot().keys() & set(plugin_main.timing_mod.TIMING_FIELDS))


class TestWiringRecords(WiringTestBase):
    async def test_drop_record_has_no_allow_and_stops_there(self):
        plugin = self.build(debug_timing=True)
        now = time.time()
        sent_at = now - 310  # 平台发出时间（raw time）
        received_at = now - 10  # AstrBot 接收时间：比平台发出晚了 300 秒
        event = FakeEvent(
            message_ts=received_at,
            raw_event={"time": sent_at},
            message_id="mid-1",
            chain=[Reply(id="q-9")],
        )
        await plugin.gate(event)
        await plugin.apply_reply_delay(event)  # 被 stop 的事件就算被叫到装饰阶段也不该再写

        records = self.read()
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["action"], "drop")
        self.assertIsNone(rec["t_allow"])  # DROP 没有 t_allow
        # t_recv 取的是平台发出时间（raw time），不是 AstrBot 接收时间
        self.assertEqual(rec["t_recv"], sent_at)
        self.assertNotEqual(rec["t_recv"], received_at)
        # 只有取发出时间，t_recv_wall - t_recv 才是「平台侧积压」≈ 310 秒
        self.assertAlmostEqual(rec["t_recv_wall"] - rec["t_recv"], 310.0, delta=1.0)

        self.assertEqual(rec["message_id"], "mid-1")
        self.assertEqual(rec["quoted_id"], "q-9")
        self.assertIsNone(rec["t_decorate"])
        self.assertIsNone(rec["delay"])
        self.assertIsNone(rec["t_ready"])
        self.assertIsNone(rec["batch"])
        self.assertLessEqual(rec["t_recv_wall"], rec["t_gate"])  # 开始处理 ≤ 判定完成
        self.assertEqual(rec["umo"], UMO)
        self.assertEqual(rec["sender_id"], "2001")
        self.assertEqual(rec["kind"], "chime")

    async def test_missing_platform_fields_are_null_and_empty(self):
        plugin = self.build(debug_timing=True)
        event = FakeEvent(message_ts=0.0)  # raw time 和接收时间都没有、无 message_id、无引用
        await plugin.gate(event)
        rec = self.read()[0]
        self.assertIsNone(rec["t_recv"])
        self.assertEqual(rec["message_id"], "")
        self.assertEqual(rec["quoted_id"], "")

    async def test_allow_is_written_at_decorate_with_full_timing(self):
        plugin = self.build(
            debug_timing=True,
            base_probability={"chime": 1.0, "proactive": 0.3, "addressed": 1.0},
            reply_delay={"enable": True, "min_seconds": 0.05, "max_seconds": 0.05},
        )
        event = FakeEvent()
        await plugin.gate(event)
        self.assertEqual(self.read(), [])  # 放行后还没到发送阶段，先不落盘

        await plugin.apply_reply_delay(event)
        records = self.read()
        self.assertEqual(len(records), 1)  # 一条消息只写一行
        rec = records[0]
        self.assertEqual(rec["action"], "allow")
        self.assertEqual(rec["reason"], "allow")
        self.assertIsNotNone(rec["t_allow"])
        self.assertIsNotNone(rec["t_decorate"])
        self.assertIsNotNone(rec["t_ready"])
        self.assertGreaterEqual(rec["delay"], 0.05)  # 真的 sleep 了，秒数记下来
        self.assertLessEqual(rec["t_allow"], rec["t_decorate"])
        self.assertLessEqual(rec["t_decorate"], rec["t_ready"])
        self.assertIsNone(rec["batch"])  # 普通路径

    async def test_allow_without_delay_records_zero(self):
        plugin = self.build(
            debug_timing=True,
            base_probability={"chime": 1.0, "proactive": 0.3, "addressed": 1.0},
            reply_delay={"enable": False},
        )
        event = FakeEvent()
        await plugin.gate(event)
        await plugin.apply_reply_delay(event)
        rec = self.read()[0]
        self.assertEqual(rec["delay"], 0.0)

    async def test_allowed_but_never_sent_is_still_recorded(self):
        # 放行了但事件在装饰阶段没有结果（被别的插件吃掉）：记录照落，t_decorate 留 null
        plugin = self.build(
            debug_timing=True, base_probability={"chime": 1.0, "proactive": 0.3, "addressed": 1.0}
        )
        event = FakeEvent()
        await plugin.gate(event)
        await plugin.apply_reply_delay(FakeEvent(with_result=False, message_ts=0.0))
        self.assertEqual(self.read(), [])  # 另一个事件没有挂记录 → 不该写

        event2 = FakeEvent()
        await plugin.gate(event2)
        event2._result = None
        await plugin.apply_reply_delay(event2)
        rec = self.read()[0]
        self.assertEqual(rec["action"], "allow")
        self.assertIsNone(rec["t_decorate"])  # 没有真要走发送
        self.assertIsNone(rec["delay"])
        self.assertIsNone(rec["t_ready"])

    async def test_queue_record_has_allow(self):
        plugin = self.build(debug_timing=True, time_weights="0-24=0")  # 睡眠时段
        event = FakeEvent(at_or_wake=True, text="睡了吗")
        await plugin.gate(event)
        rec = self.read()[0]
        self.assertEqual(rec["action"], "queue")
        self.assertIsNotNone(rec["t_allow"])
        self.assertIsNone(rec["t_decorate"])

    async def test_batch_fold_and_lead_are_distinguishable(self):
        plugin = self.build(
            debug_timing=True,
            time_weights="0-24=10",  # 活跃度 0.1 < 阈值 → 走攒批
            addressed_reply={
                "enable": True,
                "immediate_threshold": 0.6,
                "batch_window_seconds": 0.05,
                "batch_max_messages": 10,
            },
        )
        lead_event = FakeEvent(at_or_wake=True, text="第一条")
        await plugin.gate(lead_event)  # 没有批次 → 当领队，等 0.05s 后继续走发送
        await plugin.apply_reply_delay(lead_event)
        lead = self.read()[0]
        self.assertEqual(lead["action"], "allow")
        self.assertEqual(lead["batch"], {"lead": True, "wait": 0.05, "merged": 1})

        # 换成 60 秒窗口的批次：第一条当领队（先不 await），第二条并进去
        plugin._batch = plugin_main.batch_mod.BatchPlanner(
            enabled=True, window_seconds=60.0, immediate_threshold=0.6, max_messages=10
        )
        first = FakeEvent(at_or_wake=True, text="第一条")
        leader_task = asyncio.create_task(plugin.gate(first))
        await asyncio.sleep(0.01)
        folded = FakeEvent(at_or_wake=True, text="第二条")
        await plugin.gate(folded)  # 已有未过期批次 → 并进去
        self.assertTrue(folded.is_stopped())  # 并入的那条不花 token
        rec = self.read()[-1]
        self.assertEqual(rec["action"], "allow")
        self.assertEqual(rec["batch"], {"lead": False, "wait": 0.0, "merged": 2})
        leader_task.cancel()
        try:
            await leader_task
        except asyncio.CancelledError:
            pass

    async def test_io_failure_does_not_break_the_gate(self):
        plugin = self.build(
            debug_timing=True, base_probability={"chime": 1.0, "proactive": 0.3, "addressed": 1.0}
        )

        def boom(_record):
            raise OSError("埋点炸了")

        plugin._timing_sink.write = boom
        allowed = FakeEvent()
        await plugin.gate(allowed)
        await plugin.apply_reply_delay(allowed)  # 走到落盘 → 被 monkeypatch 的写入炸掉
        self.assertFalse(allowed.is_stopped())  # 判定行为不变（chime 概率 1.0 → 放行）
        self.assertEqual(plugin.stats_snapshot()["allows"], 1)
        self.assertTrue(any("埋点写入异常" in msg for msg in LOGGER.messages("warning")))
        self.assertFalse(self.path.exists())

    async def test_record_failure_does_not_break_the_gate(self):
        plugin = self.build(
            debug_timing=True, base_probability={"chime": 1.0, "proactive": 0.3, "addressed": 1.0}
        )
        original = plugin_main.timing_mod.normalize_record

        def boom(*_args, **_kwargs):
            raise RuntimeError("构造记录炸了")

        plugin_main.timing_mod.normalize_record = boom
        try:
            event = FakeEvent()
            await plugin.gate(event)
            await plugin.apply_reply_delay(event)
        finally:
            plugin_main.timing_mod.normalize_record = original

        self.assertFalse(event.is_stopped())
        self.assertEqual(plugin.stats_snapshot()["allows"], 1)
        self.assertTrue(any("埋点构造失败" in msg for msg in LOGGER.messages("warning")))
        self.assertEqual(self.read(), [])

    async def test_event_without_extras_uses_attribute_stash(self):
        # AstrBot 版本差异：没有 set_extra/get_extra 时退化成私有属性也要能跑通
        plugin = self.build(
            debug_timing=True, base_probability={"chime": 1.0, "proactive": 0.3, "addressed": 1.0}
        )
        saved_set = FakeEvent.set_extra
        saved_get = FakeEvent.get_extra
        event = FakeEvent()
        del FakeEvent.set_extra
        del FakeEvent.get_extra
        try:
            await plugin.gate(event)
            await plugin.apply_reply_delay(event)
        finally:
            FakeEvent.set_extra = saved_set
            FakeEvent.get_extra = saved_get
        rec = self.read()[0]
        self.assertEqual(rec["action"], "allow")
        self.assertIsNotNone(rec["t_decorate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
