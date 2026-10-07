"""回复闸门：作息概率放行 + 已读不回（防两个 bot 互刷烧 token）。

    收到消息
      → 静默名单命中？          → stop（已读不回，打日志）
      → 循环熔断中？            → stop（打日志）
      → 睡眠时段？
          ├ 被点名 / 私聊      → 入队，起床补发
          └ 群聊闲聊            → stop
      → 加权概率放行？
          ├ 通过                → 正常走 pipeline（并放开内置 active_reply 概率）
          └ 丢弃                → stop（计入连续丢弃）

判定逻辑全在 ``core/`` 里（纯函数 + 有状态闸门，无框架依赖，可单测）；
本文件只做**事件适配和钩子注册**，不写判定规则。

三条已核对的 AstrBot 事实（详见 docs/可行性报告与设计.md 与 README）：

1. handler 在 WakingCheckStage 只是**被收集**进 ``event.extras["activated_handlers"]``
   （waking_check/stage.py:174-253），**真正执行**在 ProcessStage
   （process_stage/method/star_request.py:36-52），而那个循环**每轮开头**就
   ``if event.is_stopped(): break``。所以本插件 priority 更大 → 先执行 →
   ``stop_event()`` 会让后面的内置 handler 和 LLM（process_stage/stage.py:52-66）
   一起被跳过 → **token 消耗为 0**。
2. ``active_reply.possibility_reply`` 每轮重读
   （group_chat_context.py:58-69 的 ``cfg()`` → ``get_config()`` → 缓存中的同一对象）
   且 ``AstrBotConfig`` 是 dict 子类、没有 ``__setitem__`` → 写内存不落盘。
3. ``is_at_or_wake_command`` 在「唤醒前缀 / 被 @ / @全体 / 引用机器人 / 私聊」时被置位
   （waking_check/stage.py:128,149,158），是判断「被点名」的准确依据。

**绝不调 save_config()**：写了就是纯内存，落了盘就等于把当轮的随机结果焊进配置。
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .core import classify, coerce, queue as queue_mod, schedule
from .core.delay import DelayConfig, reply_delay_seconds
from .core.gate import Action, Gate, GateConfig, MessageInfo, MessageKind

PLUGIN_NAME = "astrbot_plugin_reply_gate"

# 内置那个调 need_active_reply 的 on_message 没写 priority（默认 0），
# 内置 persist_group_message 是 maxsize-2。996 足够跑赢 on_message；
# 不去抢 persist 的位置是**故意的**：已读不回只该省 LLM 的钱，
# 消息本身还是要入历史（不然「已读」这个语义都不成立）。
GATE_PRIORITY = 996

FLUSH_TICK_SECONDS = 30


@register(
    PLUGIN_NAME,
    "NS9927",
    "作息概率放行 + 已读不回（防两个 bot 互刷烧 token）",
    "v0.2.0",
)
class ReplyGate(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._rng = random.Random()

        self._gate = Gate()
        self._queue = queue_mod.SleepQueue()
        self._segments = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_flush_date = None
        self._warned: set[str] = set()

        self._apply_config()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _apply_config(self) -> None:
        """把配置读进内存对象。配置写坏一律退回默认值，绝不让插件加载失败。"""
        raw = dict(self.config) if hasattr(self.config, "keys") else {}

        self._enable = coerce.as_bool(raw.get("enable"), True)
        self._verbose = coerce.as_bool(raw.get("verbose_log"), True)
        self._session_whitelist = coerce.as_id_list(raw.get("session_whitelist"))

        try:
            self._segments = schedule.parse_time_weights(
                str(raw.get("time_weights") or schedule.DEFAULT_TIME_WEIGHTS)
            )
        except schedule.TimeWeightsError as exc:
            logger.error("[%s] time_weights 配置有误，回退默认作息表：%s", PLUGIN_NAME, exc)
            self._segments = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)

        self._interpolate_minutes = coerce.as_float(raw.get("interpolate_minutes"), 45.0)
        self._weekend_multiplier = coerce.as_float(raw.get("weekend_multiplier"), 1.3)
        self._weekend_shift = coerce.as_float(raw.get("weekend_sleep_shift_hours"), 1.0)

        self._gate.config = GateConfig.from_raw(raw)
        self._delay_cfg = DelayConfig.from_raw(raw.get("reply_delay"))

        sleep_cfg = coerce.as_mapping(raw.get("sleep_queue"))
        self._flush_hour = coerce.as_int(sleep_cfg.get("flush_at_hour"), 8)
        self._queue.max_per_session = max(1, coerce.as_int(sleep_cfg.get("max_per_session"), 5))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        self._start_flush_loop()
        logger.info(
            "[%s] 已加载：总开关=%s 作息段=%d 睡眠补发=%s:00 闸门=%s",
            PLUGIN_NAME,
            "开" if self._enable else "关",
            len(self._segments),
            f"{self._flush_hour:02d}",
            "开" if self._gate.config.loop_breaker_enable else "关",
        )

    async def terminate(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("[%s] 已卸载", PLUGIN_NAME)

    def _start_flush_loop(self) -> None:
        """幂等启动补发循环（initialize 会调，首次消息也会兜底调一次）。"""
        if self._task and not self._task.done():
            return
        try:
            self._running = True
            self._task = asyncio.get_running_loop().create_task(
                self._flush_loop(), name=f"{PLUGIN_NAME}-flush"
            )
        except RuntimeError:
            # 没有正在运行的事件循环（理论上不该发生），跳过即可，不影响拦消息
            self._running = False
            self._task = None

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(FLUSH_TICK_SECONDS)
                if not self._running:
                    break
                await self._maybe_flush()
            except asyncio.CancelledError:
                raise  # 不要吞，否则 terminate() 的 await 会挂住
            except Exception:
                logger.exception("[%s] 补发循环异常，60s 后重试", PLUGIN_NAME)
                await asyncio.sleep(60)

    async def _maybe_flush(self) -> None:
        """过了起床整点就把睡眠期积压的点名消息补发出去（每天一次）。"""
        if not self._queue:
            return
        now = datetime.now()
        if not queue_mod.due_for_flush(now, self._flush_hour, self._last_flush_date):
            return
        self._last_flush_date = now.date()

        pending = self._queue.drain(now.timestamp())
        if not pending:
            return

        grouped: dict[str, list[queue_mod.QueuedMessage]] = {}
        for msg in pending:
            grouped.setdefault(msg.umo, []).append(msg)

        logger.info(
            "[%s] 起床补发：%d 条消息 / %d 个会话", PLUGIN_NAME, len(pending), len(grouped)
        )
        for umo, messages in grouped.items():
            text = queue_mod.compose_flush_text(messages)
            try:
                await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
            except Exception:
                logger.exception("[%s] 补发失败 session=%s", PLUGIN_NAME, umo)

    # ------------------------------------------------------------------
    # 闸门总入口
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=GATE_PRIORITY)
    async def gate(self, event: AstrMessageEvent):
        """判定链入口。任何异常都吃掉——闸门坏了只能放行，不能把机器人变成哑巴。"""
        try:
            await self._decide(event)
        except Exception:
            logger.exception("[%s] 闸门异常，本条放行（fail-open）", PLUGIN_NAME)

    async def _decide(self, event: AstrMessageEvent) -> None:
        if not self._enable:
            return
        self._start_flush_loop()  # 兜底：万一 initialize 没被调用

        umo = event.unified_msg_origin
        if self._session_whitelist and umo not in self._session_whitelist:
            return

        sender_id = event.get_sender_id()
        if classify.is_self_message(sender_id, event.get_self_id()):
            return

        kind = classify.classify(
            is_private=event.is_private_chat(),
            at_or_wake=bool(getattr(event, "is_at_or_wake_command", False)),
        )

        now = datetime.now()
        activity = schedule.activity_ratio(
            now,
            self._segments,
            interpolate_minutes=self._interpolate_minutes,
            weekend_multiplier=self._weekend_multiplier,
            weekend_sleep_shift_hours=self._weekend_shift,
        )
        asleep = schedule.sleeping(now, self._segments, weekend_sleep_shift_hours=self._weekend_shift)

        info = MessageInfo(
            umo=umo,
            sender_id=sender_id,
            kind=kind,
            sender_name=event.get_sender_name(),
            text=event.get_message_str(),
            timestamp=time.time(),
        )
        decision = self._gate.evaluate(info, activity=activity, sleeping=asleep)

        if decision.action is Action.DROP:
            event.stop_event()
            self._log_decision(decision, event, now)
            return

        if decision.action is Action.QUEUE:
            result = self._queue.push(
                queue_mod.QueuedMessage(
                    umo=umo,
                    sender_id=sender_id,
                    sender_name=info.sender_name,
                    text=info.text,
                    timestamp=info.timestamp,
                    kind=kind,
                )
            )
            event.stop_event()
            detail = f"队列 {len(self._queue)} 条"
            if result.evicted is not None:
                detail += f"，挤掉最旧一条「{result.evicted.text[:20]}」"
            if self._verbose:
                logger.info(
                    "[%s] 睡眠入队 %s | %s | %s",
                    PLUGIN_NAME,
                    umo,
                    decision.log_line(),
                    detail,
                )
            return

        # 放行：群聊插话这一路必须把内置概率顶开，否则会被二次掷骰子（概率被平方）
        opened: float | None = None
        if kind is MessageKind.CHIME:
            opened = self._open_active_reply(event)

        if self._verbose:
            logger.info(
                "[%s] 放行 %s | %s | 内置 active_reply 概率=%s | %s",
                PLUGIN_NAME,
                umo,
                decision.log_line(),
                "已顶到 1.0" if opened == 1.0 else ("无" if opened is None else f"{opened}"),
                info.text[:40],
            )

    def _open_active_reply(self, event: AstrMessageEvent) -> float | None:
        """把内置 ``active_reply.possibility_reply`` 顶到 1.0，返回**回读值**。

        内置的判定（group_chat_context.need_active_reply）每轮重读这个值，
        而我们**已经**掷过骰子了（基础概率 × 时段活跃度）。这里不再掷第二次，
        所以写 1.0 = 「这一轮我批准了，你直接放行」。

        只写内存对象，**不调 save_config()**。

        返回回读值是为了排障：2026-10-07 真机验证时，就是靠「写了 1.0 但内置没插话」
        这个现象才定位到问题的。写不到就返回 None。
        """
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            ltm = cfg.get("provider_ltm_settings") if hasattr(cfg, "get") else None
            active = ltm.get("active_reply") if isinstance(ltm, dict) else None
            if not isinstance(active, dict) or "possibility_reply" not in active:
                self._warn_once(
                    "active_reply_missing",
                    "找不到 provider_ltm_settings.active_reply.possibility_reply，"
                    "群聊插话概率改不动（内置群聊上下文感知可能没开）。",
                )
                return None
            if not coerce.as_bool(active.get("enable"), False):
                self._warn_once(
                    "active_reply_disabled",
                    "内置 active_reply.enable=false，本插件放行了机器人也不会插话，"
                    "建议在 AstrBot 配置里打开「主动回复」。",
                )
            method = str(active.get("method", ""))
            if method and method != "possibility_reply":
                self._warn_once(
                    "active_reply_method",
                    f"内置 active_reply.method={method}，不是 possibility_reply，"
                    "本插件改写的概率值不会生效。",
                )
            active["possibility_reply"] = 1.0
            readback = active["possibility_reply"]  # 纯内存写，立刻回读确认
            return float(readback) if isinstance(readback, (int, float)) else None
        except Exception:
            logger.exception("[%s] 改写 active_reply 概率失败（不影响本次放行）", PLUGIN_NAME)
            return None

    def _log_decision(self, decision, event: AstrMessageEvent, now: datetime) -> None:
        if not self._verbose:
            logger.debug("[%s] %s | %s", PLUGIN_NAME, decision.log_line(), event.unified_msg_origin)
            return
        logger.info(
            "[%s] 已读不回 %s | %s | %s | %s",
            PLUGIN_NAME,
            event.unified_msg_origin,
            decision.log_line(),
            event.get_message_str()[:40],
            schedule.describe(
                now,
                self._segments,
                interpolate_minutes=self._interpolate_minutes,
                weekend_multiplier=self._weekend_multiplier,
                weekend_sleep_shift_hours=self._weekend_shift,
            ),
        )

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning("[%s] %s", PLUGIN_NAME, message)

    # ------------------------------------------------------------------
    # 回复延迟 + 会话冷却计时
    # ------------------------------------------------------------------
    @filter.on_decorating_result(priority=GATE_PRIORITY)
    async def apply_reply_delay(self, event: AstrMessageEvent):
        """按作息给回复加延迟，替代固定秒回。

        只做延迟，不碰 @ 和引用的插入——那是 astrbot_plugin_at_inline 的职责。
        """
        if not self._enable:
            return
        if event.is_stopped() or event.get_result() is None:
            return
        umo = event.unified_msg_origin
        if self._session_whitelist and umo not in self._session_whitelist:
            return

        # 延迟是可选的，但会话冷却的计时不受它开关影响：
        # 关了延迟也得记「刚回过一条」，否则冷却永远不触发。
        if self._delay_cfg.enable:
            now = datetime.now()
            activity = schedule.activity_ratio(
                now,
                self._segments,
                interpolate_minutes=self._interpolate_minutes,
                weekend_multiplier=self._weekend_multiplier,
                weekend_sleep_shift_hours=self._weekend_shift,
            )
            asleep = schedule.sleeping(
                now, self._segments, weekend_sleep_shift_hours=self._weekend_shift
            )
            seconds = reply_delay_seconds(
                activity, config=self._delay_cfg, sleeping=asleep, rng=self._rng
            )
            if seconds > 0:
                if self._verbose:
                    logger.debug(
                        "[%s] 回复延迟 %.1fs（活跃度 %.2f%s）",
                        PLUGIN_NAME,
                        seconds,
                        activity,
                        "，睡眠中" if asleep else "",
                    )
                await asyncio.sleep(seconds)

        # 真的发出去一条，才计入会话冷却
        self._gate.record_reply(umo)
