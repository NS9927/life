"""webapi.py 单测：插件页面的后端接口（曲线读写、预览、设置白名单）。

不需要 AstrBot：webapi 只依赖 astrbot.api.web.request 这个代理，
测试里用桩替换（见 tests/_astrbot_stub.py 的 FakeRequest）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "life"))

from tests._astrbot_stub import install, web_request  # noqa: E402

install()  # 必须在 import webapi 之前

from life.core import schedule  # noqa: E402
from life.webapi import (  # noqa: E402
    CURVE_POINTS,
    PREVIEW_STEP_MINUTES,
    WebApi,
    curve_from_segments,
    curve_to_time_weights,
    preview_series,
    segments_from_weights,
)


class SaveableConfig(dict):
    """能数落盘次数的假配置对象（真身是 AstrBotConfig）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0

    def save_config(self) -> None:
        self.save_calls += 1


class FakeCtx:
    """照抄真实 register_web_api 的去重语义：同路由替换，不重复追加。"""

    def __init__(self):
        self.registered: list[tuple] = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered = [r for r in self.registered if r[0] != route]
        self.registered.append((route, handler, tuple(methods), desc))


class FakePlugin:
    """只实现 webapi 用到的那几个属性。"""

    def __init__(self):
        self.context = FakeCtx()
        self.config = SaveableConfig(
            {
                "enable": True,
                "time_weights": schedule.DEFAULT_TIME_WEIGHTS,
                "interpolate_minutes": 45,
                "weekend_multiplier": 1.3,
                "weekend_sleep_shift_hours": 1,
                "base_probability": {"chime": 0.03, "proactive": 0.3, "addressed": 1.0},
                "consecutive_drop_limit": 3,
                "consecutive_drop_scope": "sender",
                "session_cooldown": {"enable": True, "window_seconds": 60},
                "sleep_queue": {"enable": True, "flush_at_hour": 8},
                "reply_delay": {"enable": True, "max_seconds": 8.0},
                "loop_breaker": {"enable": True},
                "addressed_reply": {"enable": True, "immediate_threshold": 0.6},
            }
        )
        self._apply_config()

    def _apply_config(self) -> None:
        self._segments = schedule.parse_time_weights(self.config["time_weights"])
        self._interpolate_minutes = float(self.config["interpolate_minutes"])
        self._weekend_multiplier = float(self.config["weekend_multiplier"])
        self._weekend_shift = float(self.config["weekend_sleep_shift_hours"])

    def stats_snapshot(self) -> dict:
        return {"drops": 3, "allows": 2, "queued": 0, "flushed": 0, "batched": 1}


def flat(value: float) -> list[float]:
    return [value] * CURVE_POINTS


class TestPureHelpers(unittest.TestCase):
    def setUp(self):
        self.segments = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)

    def test_curve_length(self):
        self.assertEqual(len(curve_from_segments(self.segments)), CURVE_POINTS)

    def test_curve_sampling_matches_table(self):
        curve = curve_from_segments(self.segments)
        self.assertEqual(curve[0], 80.0)  # 00:00 在 0-2=80
        self.assertEqual(curve[4], 0.0)  # 02:00 睡眠
        self.assertEqual(curve[9], 0.0)  # 04:30 睡眠
        self.assertEqual(curve[20], 30.0)  # 10:00
        self.assertEqual(curve[36], 30.0)  # 18:00 还在 14-19=30
        self.assertEqual(curve[38], 80.0)  # 19:00 进 19-23=80
        self.assertEqual(curve[40], 80.0)  # 20:00

    def test_round_trip_curve_to_text_to_curve(self):
        original = [float(i) for i in range(CURVE_POINTS)]
        text = curve_to_time_weights(original)
        self.assertEqual(len(text.splitlines()), CURVE_POINTS)
        self.assertEqual(curve_from_segments(segments_from_weights(original)), original)

    def test_curve_text_is_parseable(self):
        text = curve_to_time_weights(flat(50.0))
        segs = schedule.parse_time_weights(text)
        self.assertEqual(len(segs), CURVE_POINTS)
        self.assertAlmostEqual(segs[0].start, 0.0)
        self.assertAlmostEqual(segs[-1].end, 24.0)
        self.assertTrue(all(s.weight == 50.0 for s in segs))

    def test_curve_clamped(self):
        text = curve_to_time_weights([-5.0] * CURVE_POINTS)
        self.assertIn("=0", text)
        text = curve_to_time_weights([999.0] * CURVE_POINTS)
        self.assertIn("=100", text)

    def test_preview_length_and_extremes(self):
        series = preview_series(segments_from_weights(flat(100.0)), 15)
        self.assertEqual(len(series), int(24 * 60 / PREVIEW_STEP_MINUTES))
        self.assertTrue(all(abs(v - 1.0) < 1e-9 for v in series))
        series = preview_series(segments_from_weights(flat(0.0)), 15)
        self.assertTrue(all(v == 0.0 for v in series))

    def test_preview_smooths_around_cut(self):
        weights = flat(0.0)
        weights[20] = 100.0  # 10:00 一个尖峰
        series = preview_series(segments_from_weights(weights), 45)
        peak = max(series)
        self.assertLess(peak, 1.0)  # 插值后不该到顶
        self.assertGreater(peak, 0.2)


class TestRoutes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = FakePlugin()
        self.api = WebApi(self.plugin, version="v9.9.9")

    async def test_register_four_routes(self):
        count = self.api.register()
        self.assertEqual(count, 4)
        routes = {r[0]: r[2] for r in self.plugin.context.registered}
        self.assertEqual(routes["/life/page/config"], ("GET",))
        self.assertEqual(routes["/life/page/curve"], ("POST",))
        self.assertEqual(routes["/life/page/preview"], ("POST",))
        self.assertEqual(routes["/life/page/settings"], ("POST",))

    async def test_register_is_idempotent(self):
        self.api.register()
        self.api.register()
        self.assertEqual(len(self.plugin.context.registered), 4)


class TestGetConfig(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = FakePlugin()
        self.api = WebApi(self.plugin, version="v9.9.9")

    async def test_shape(self):
        data = await self.api.get_config()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["curve"]), CURVE_POINTS)
        self.assertEqual(data["plugin_version"], "v9.9.9")
        self.assertIn("config", data)
        self.assertIn("label", data["now"])
        self.assertIn("活跃度", data["now"]["label"])
        self.assertEqual(data["stats"]["drops"], 3)
        self.assertEqual(data["interpolate_minutes"], 45)

    async def test_now_activity_between_zero_and_one(self):
        data = await self.api.get_config()
        self.assertGreaterEqual(data["now"]["activity"], 0.0)
        self.assertLessEqual(data["now"]["activity"], 1.0)
        self.assertIsInstance(data["now"]["sleeping"], bool)


class TestPostCurve(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = FakePlugin()
        self.api = WebApi(self.plugin, version="v9.9.9")

    async def test_saves_and_applies_immediately(self):
        weights = flat(0.0)
        for index in range(38, CURVE_POINTS):  # 19:00 之后 80
            weights[index] = 80.0
        web_request.payload = {
            "weights": weights,
            "interpolate_minutes": 15,
            "weekend_multiplier": 1.5,
        }
        data = await self.api.post_curve()

        self.assertTrue(data["ok"])
        self.assertEqual(self.plugin.config.save_calls, 1)  # 落盘一次
        self.assertEqual(self.plugin._interpolate_minutes, 15.0)  # 立即生效
        self.assertEqual(self.plugin._weekend_multiplier, 1.5)
        self.assertEqual(data["curve"], weights)  # 回读一致
        self.assertIn("time_weights", self.plugin.config)
        # 解析后的作息表确实变了
        self.assertAlmostEqual(schedule.weight_at(20.0, self.plugin._segments), 80.0)
        self.assertAlmostEqual(schedule.weight_at(12.0, self.plugin._segments), 0.0)

    async def test_rejects_wrong_length(self):
        web_request.payload = {"weights": [1, 2, 3]}
        data = await self.api.post_curve()
        self.assertFalse(data["ok"])
        self.assertEqual(self.plugin.config.save_calls, 0)

    async def test_rejects_missing_body(self):
        web_request.payload = {}
        data = await self.api.post_curve()
        self.assertFalse(data["ok"])

    async def test_clamps_out_of_range(self):
        weights = flat(0.0)
        weights[0] = 999.0
        weights[1] = -50.0
        web_request.payload = {"weights": weights}
        data = await self.api.post_curve()
        self.assertEqual(data["curve"][0], 100.0)
        self.assertEqual(data["curve"][1], 0.0)


class TestPostPreview(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = FakePlugin()
        self.api = WebApi(self.plugin, version="v9.9.9")

    async def test_preview_with_weights(self):
        web_request.payload = {"weights": flat(100.0), "interpolate_minutes": 0}
        data = await self.api.post_preview()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["series"]), 96)
        self.assertEqual(data["step_minutes"], 15)
        self.assertTrue(all(v == 1.0 for v in data["series"]))

    async def test_preview_falls_back_to_current_segments(self):
        web_request.payload = {}
        data = await self.api.post_preview()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["series"]), 96)

    async def test_preview_does_not_save(self):
        web_request.payload = {"weights": flat(100.0)}
        await self.api.post_preview()
        self.assertEqual(self.plugin.config.save_calls, 0)


class TestPostSettings(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = FakePlugin()
        self.api = WebApi(self.plugin, version="v9.9.9")

    async def test_saves_whitelisted_keys_only(self):
        web_request.payload = {
            "consecutive_drop_limit": 5,
            "verbose_log": False,
            "恶意键": "x",
            "session_whitelist": ["aiocqhttp:GroupMessage:1"],
        }
        data = await self.api.post_settings()
        self.assertTrue(data["ok"])
        self.assertEqual(self.plugin.config["consecutive_drop_limit"], 5)
        self.assertFalse(self.plugin.config["verbose_log"])
        self.assertNotIn("恶意键", self.plugin.config)
        self.assertEqual(self.plugin.config.save_calls, 1)
        self.assertIn("consecutive_drop_limit", data["saved"])
        self.assertNotIn("恶意键", data["saved"])

    async def test_nested_section_is_merged_not_replaced(self):
        web_request.payload = {"reply_delay": {"max_seconds": 12.0}}
        data = await self.api.post_settings()
        self.assertTrue(data["ok"])
        self.assertEqual(self.plugin.config["reply_delay"]["max_seconds"], 12.0)
        self.assertTrue(self.plugin.config["reply_delay"]["enable"])  # 没被抹掉

    async def test_applies_immediately(self):
        web_request.payload = {"base_probability": {"chime": 0.5}}
        data = await self.api.post_settings()
        self.assertTrue(data["ok"])
        # webapi 负责写配置 + 立即 _apply_config；真机上下一条消息就会用新概率
        self.assertEqual(self.plugin.config["base_probability"]["chime"], 0.5)
        self.assertEqual(self.plugin.config["base_probability"]["proactive"], 0.3)  # 没被抹

    async def test_empty_body_rejected(self):
        web_request.payload = {}
        data = await self.api.post_settings()
        self.assertFalse(data["ok"])
        self.assertEqual(self.plugin.config.save_calls, 0)

    async def test_non_dict_body_rejected(self):
        web_request.payload = ["not", "a", "dict"]
        data = await self.api.post_settings()
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
