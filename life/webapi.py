"""插件页面的后端接口。

AstrBot v4.28.2 的插件页面机制（已核对源码）：

- 前端页面放 ``pages/<页面名>/index.html``，框架自动发现
  （``dashboard/services/plugin_page_service.py:508 discover_plugin_pages``），
  **侧边栏标题就是目录名**，而 ``normalize_plugin_page_name`` 只禁 ``.``/``..``/斜杠，
  所以目录名可以直接写中文。
- bridge SDK 自动注入，前端用 ``window.AstrBotPluginPage.apiGet/apiPost("page/xxx")`` 调后端。
- 后端用 ``context.register_web_api("/life/page/xxx", handler, ["GET"], "描述")`` 注册
  （``core/star/context.py:705``）；**handler 只收 path 参数**，请求体从
  ``astrbot.api.web.request`` 这个上下文代理里拿。
- 前端实际请求的完整地址是 ``/api/plug/life/page/xxx``。

设计原则：**所有接口只读写本插件自己的配置**，绝不碰 AstrBot 的内置配置。
每次保存后立刻调 ``plugin._apply_config()``，所以页面上改完**立即生效，不用重启机器人**。
"""
from __future__ import annotations

from datetime import datetime

from astrbot.api import logger
from astrbot.api.web import request

from .core import coerce, schedule

PLUGIN_NAME = "life"
API_PREFIX = f"/{PLUGIN_NAME}/page"

CURVE_POINTS = 48  # 折线图节点数：每半小时一个
PREVIEW_STEP_MINUTES = 15  # 预览曲线分辨率（96 个点）

# 「活跃度曲线」页面顺带可改的三个参数
CURVE_PARAM_KEYS = (
    "interpolate_minutes",
    "weekend_multiplier",
    "weekend_sleep_shift_hours",
)

# 「闸门设置」页面允许写入的键（白名单，页面传什么都不许越界）
SETTINGS_KEYS = frozenset(
    {
        "enable",
        "verbose_log",
        "time_weights",
        "interpolate_minutes",
        "weekend_multiplier",
        "weekend_sleep_shift_hours",
        "base_probability",
        "consecutive_drop_limit",
        "consecutive_drop_scope",
        "silence_list",
        "session_whitelist",
        "session_cooldown",
        "sleep_queue",
        "reply_delay",
        "loop_breaker",
        "addressed_reply",
        "addressed_max_age_seconds",
        "proactive",
        "debug_timing",
    }
)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ----------------------------------------------------------------------
# 纯函数：曲线 <-> 作息表文本
# ----------------------------------------------------------------------
def curve_from_segments(segments: list[schedule.Segment]) -> list[float]:
    """把作息表采样成 48 个半小时节点的原始权重（不插值）。"""
    out: list[float] = []
    for index in range(CURVE_POINTS):
        seg = schedule.segment_at(index * 0.5, segments)
        out.append(round(seg.weight, 1) if seg else 0.0)
    return out


def curve_to_time_weights(weights: list[float]) -> str:
    """48 个半小时权重 → ``time_weights`` 配置文本。"""
    lines = []
    for index in range(CURVE_POINTS):
        start = index * 0.5
        end = start + 0.5
        weight = _clamp(coerce.as_float(weights[index], 0.0), 0.0, 100.0)
        lines.append(f"{start:g}-{end:g}={weight:g}")
    return "\n".join(lines)


def segments_from_weights(weights: list[float]) -> list[schedule.Segment]:
    return schedule.parse_time_weights(curve_to_time_weights(weights))


def preview_series(segments: list[schedule.Segment], interpolate_minutes: float) -> list[float]:
    """插件**实际会用**的插值后活跃度（0~1），每 15 分钟一个点，共 96 个。

    画基线的（不含周末系数）——周末是整体乘系数，不改变形状。
    """
    total = int(24 * 60 / PREVIEW_STEP_MINUTES)
    series = []
    for index in range(total):
        hours = index * PREVIEW_STEP_MINUTES / 60.0
        ratio = schedule.weight_at(hours, segments, interpolate_minutes) / 100.0
        series.append(round(_clamp(ratio, 0.0, 1.0), 4))
    return series


# ----------------------------------------------------------------------
# 接口
# ----------------------------------------------------------------------
class WebApi:
    """插件页面用的后端接口。handler 只收 path 参数，body 走 request 代理。"""

    def __init__(self, plugin, version: str = "") -> None:
        self.plugin = plugin
        self.version = version

    # ---- 注册 --------------------------------------------------------
    def register(self) -> int:
        register = self.plugin.context.register_web_api
        routes = [
            (f"{API_PREFIX}/config", self.get_config, ["GET"], "life: 读取配置、曲线与运行状态"),
            (f"{API_PREFIX}/curve", self.post_curve, ["POST"], "life: 保存作息活跃度曲线"),
            (f"{API_PREFIX}/preview", self.post_preview, ["POST"], "life: 预览插值后的活跃度曲线"),
            (f"{API_PREFIX}/settings", self.post_settings, ["POST"], "life: 保存闸门设置"),
        ]
        for route, handler, methods, desc in routes:
            register(route, handler, methods, desc)
        return len(routes)

    # ---- GET ---------------------------------------------------------
    async def get_config(self) -> dict:
        plugin = self.plugin
        now = datetime.now()
        activity = schedule.activity_ratio(
            now,
            plugin._segments,
            interpolate_minutes=plugin._interpolate_minutes,
            weekend_multiplier=plugin._weekend_multiplier,
            weekend_sleep_shift_hours=plugin._weekend_shift,
        )
        sleeping = schedule.sleeping(
            now, plugin._segments, weekend_sleep_shift_hours=plugin._weekend_shift
        )
        config = dict(plugin.config) if hasattr(plugin.config, "keys") else {}
        return {
            "ok": True,
            "config": config,
            "curve": curve_from_segments(plugin._segments),
            "interpolate_minutes": plugin._interpolate_minutes,
            "weekend_multiplier": plugin._weekend_multiplier,
            "weekend_sleep_shift_hours": plugin._weekend_shift,
            "now": {
                "hours": round(now.hour + now.minute / 60.0 + now.second / 3600.0, 3),
                "activity": round(activity, 4),
                "sleeping": sleeping,
                "label": schedule.describe(
                    now,
                    plugin._segments,
                    interpolate_minutes=plugin._interpolate_minutes,
                    weekend_multiplier=plugin._weekend_multiplier,
                    weekend_sleep_shift_hours=plugin._weekend_shift,
                ),
            },
            "stats": plugin.stats_snapshot() if hasattr(plugin, "stats_snapshot") else {},
            "plugin_version": self.version,
        }

    # ---- POST --------------------------------------------------------
    async def post_curve(self) -> dict:
        body = await request.json(default={}) or {}
        weights = body.get("weights") if isinstance(body, dict) else None
        if not isinstance(weights, (list, tuple)) or len(weights) != CURVE_POINTS:
            return {"ok": False, "message": f"weights 必须是 {CURVE_POINTS} 个数的数组"}

        updates: dict = {"time_weights": curve_to_time_weights(list(weights))}
        for key in CURVE_PARAM_KEYS:
            if isinstance(body, dict) and key in body:
                updates[key] = body[key]

        await self._save(updates)
        return {
            "ok": True,
            "time_weights": updates["time_weights"],
            "curve": curve_from_segments(self.plugin._segments),
        }

    async def post_preview(self) -> dict:
        body = await request.json(default={}) or {}
        weights = body.get("weights") if isinstance(body, dict) else None
        interpolate = coerce.as_float(
            body.get("interpolate_minutes") if isinstance(body, dict) else None,
            self.plugin._interpolate_minutes,
        )

        if isinstance(weights, (list, tuple)) and len(weights) == CURVE_POINTS:
            try:
                segments = segments_from_weights(list(weights))
            except schedule.TimeWeightsError as exc:
                return {"ok": False, "message": str(exc)}
        else:
            segments = self.plugin._segments

        return {
            "ok": True,
            "series": preview_series(segments, interpolate),
            "step_minutes": PREVIEW_STEP_MINUTES,
        }

    async def post_settings(self) -> dict:
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return {"ok": False, "message": "请求体必须是 JSON 对象"}
        updates = {key: value for key, value in body.items() if key in SETTINGS_KEYS}
        if not updates:
            return {"ok": False, "message": "没有可保存的字段"}
        config = await self._save(updates)
        return {"ok": True, "config": config, "saved": sorted(updates)}

    # ---- 内部 --------------------------------------------------------
    async def _save(self, updates: dict) -> dict:
        """写进插件配置 → 立即重建内存对象 → 落盘。"""
        config = self.plugin.config
        if not hasattr(config, "get") or not hasattr(config, "__setitem__"):
            raise RuntimeError("插件配置对象不可写")

        for key, value in updates.items():
            current = config.get(key)
            if isinstance(value, dict) and isinstance(current, dict):
                # 嵌套节必须**就地合并**：整体替换会把页面没管的键抹掉
                current.update(value)
            else:
                config[key] = value

        self.plugin._apply_config()  # 立刻生效，不用重启机器人
        await self._persist()
        return dict(config) if hasattr(config, "keys") else {}

    async def _persist(self) -> None:
        config = self.plugin.config
        saver = getattr(config, "save_config_async", None)
        if callable(saver):
            try:
                await saver()
                return
            except Exception:
                logger.exception("[%s] 异步落盘失败，回退同步保存", PLUGIN_NAME)
        sync = getattr(config, "save_config", None)
        if callable(sync):
            try:
                sync()
            except Exception:
                logger.exception("[%s] 配置落盘失败（内存里的值已生效）", PLUGIN_NAME)
