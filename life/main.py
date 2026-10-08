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

``debug_timing``（默认关）：把每条被处理消息的时序写进
``plugin_data/life/timing.jsonl``，用来定位「我们放行之后到真正发送之间」的滞后
（真机案例：21:53:07 发出的回复引用了 21:47:25 的消息，滞后 342 秒）。
纯逻辑在 ``core/timing.py``，I/O 在下面的 ``TimingSink``；**埋点坏掉绝不影响闸门**。
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .core import batch as batch_mod, classify, coerce, proactive, segmented
from .core import queue as queue_mod, schedule, timing as timing_mod
from .core import typing_indicator as typing_mod
from .core.delay import DelayConfig, reply_delay_seconds
from .core.gate import Action, Gate, GateConfig, MessageInfo, MessageKind

PLUGIN_NAME = "life"
PLUGIN_VERSION = "v0.4.0"

# 内置那个调 need_active_reply 的 on_message 没写 priority（默认 0），
# 内置 persist_group_message 是 maxsize-2。996 足够跑赢 on_message；
# 不去抢 persist 的位置是**故意的**：已读不回只该省 LLM 的钱，
# 消息本身还是要入历史（不然「已读」这个语义都不成立）。
GATE_PRIORITY = 996

FLUSH_TICK_SECONDS = 30

# ----------------------------------------------------------------------
# 主动开口（PROACTIVE）
#
# 判定与调度全在 core/proactive.py（纯逻辑，可单测）；这里只做 provider 调用、
# 分段发送和后台循环的接线。安全红线：
#   1. enable=false 或 session_list 为空 → **零副作用**（不解析时间线、不起循环、
#      不调 provider）。见 _apply_config / _start_proactive_loop / _maybe_proactive_check。
#   2. schedule.sleeping(now) 为真 → 绝不主动（复用我们自己的作息表）。
#   3. provider 拿不到 / 调用失败 / 超时 → 记一条日志后跳过，**绝不重试循环、绝不阻塞**；
#      text_chat 必须包 asyncio.wait_for，理由见 docs/真机反馈与根因分析.md 第十节
#      （livingmemory 每条回复白吃 60 秒的教训）。
#   4. 同一次检查里**最多一条在飞的 LLM 调用**（顺序 await，不并发打 provider）。
# ----------------------------------------------------------------------
PROACTIVE_SEGMENT_MAX_CHARS = 120
"""主动内容分段长度上限（字符）。"""
PROACTIVE_SEGMENT_MAX_PARTS = 3
"""一次主动最多发几段（超出的并进最后一段，不丢内容）。"""
PROACTIVE_SEGMENT_DELAY_SECONDS = 1.5
"""段间停顿（秒）——真人打字也是分几条发的。"""
PROACTIVE_MIN_CHARS = 2
"""正文短于这个长度就丢弃（空串、一个标点之类）。"""
PROACTIVE_RECENT_MAX = 6
"""主动开口参考的最近聊天条数。"""
PROACTIVE_MIN_INTERVAL_SECONDS = 5.0
"""后台检查的最小间隔：防止配置写成 0 后忙等（单测会临时调小）。"""

# ----------------------------------------------------------------------
# 分段回复的顺序（方案 B）：**第一段走流水线**，其余段在第一条真正发出去之后再发
#
# 为什么：引用 / @ 是 result_decorate/stage.py:426-445（以及 at_inline）在**我们的
# on_decorating_result 之后**才加到链上的。所以「第一段我们自己发」必然丢引用，
# 而带头的那条一定是最后一段 —— 真机上用户看到的就是「先一条没头没脑的正文 →
# 再来一条带引用+@的」。方案 B 把第一段留在 result 里交给流水线，用户看到的第一条
# 就是原汁原味的「引用 + @ + 正文」。
#
# 后续段由 after_message_sent 钩子**通知**（钩子触发 = 第一条已发出），实际发送放在
# 一个后台任务里，避免把 respond 阶段按住十几秒。
# ----------------------------------------------------------------------
FOLLOWUP_EXTRA_KEY = "life_segmented_followup"
"""挂在 event 上的后续段记录（优先 extras，退化到私有属性）。"""

FOLLOWUP_HOOK_TIMEOUT_SECONDS = 30.0
"""等 ``after_message_sent`` 的上限：超时走兜底发送（先确认第一条真的出去了）。"""

# 埋点挂载在事件对象上的键（优先用 AstrBot 的 extras，退化成私有属性）
TIMING_EXTRA_KEY = "life_timing"
TIMING_FILE_NAME = "timing.jsonl"


@dataclass
class _FollowUp:
    """「第一段已发出之后要补发的其余段」的记录（由 after_message_sent 唤醒）。"""

    umo: str
    tail: tuple[str, ...]
    """第 2..N 段。"""
    delays: tuple[float, ...]
    """``TimingPlan.segment_delays``：``delays[i]`` = 第 i 段与第 i+1 段之间的间隔。

    补发第 k 段（k>=1）之前要等 ``delays[k-1]``。"""
    event: object
    signal: asyncio.Event


class TimingSink:
    """``debug_timing`` 的 jsonl 写入端（I/O 只在这里，纯逻辑在 core/timing.py）。

    - 路径：``get_astrbot_data_path()/plugin_data/life/timing.jsonl``（目录不存在就建）。
    - **任何异常一律吞掉并打一条 warning**：埋点是排障工具，绝不能因为它把闸门搞崩。
    - 行数 > 2000 或体积 > 512KB 时清空重写，只保留最近的一半
      （见 ``core/timing.trim_lines`` 的注释）。
    - 单行 append 很小，直接同步写即可，不做后台线程/大 buffering。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None  # 注入路径只为单测
        self._resolved: Path | None = None
        # 惰性数一次行数，之后自己累加：避免每次写都读整个文件
        self._lines: int | None = None

    def path(self) -> Path:
        if self._path is not None:
            return self._path
        if self._resolved is None:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            self._resolved = (
                Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / TIMING_FILE_NAME
            )
        return self._resolved

    def write(self, record) -> bool:
        """追加一行。成功 True；任何失败 False（并打 warning），**绝不抛给调用方**。"""
        try:
            line = timing_mod.encode_record(record)
            path = self.path()
            self._prepare(path)
            self._append(path, line)
        except Exception as exc:
            logger.warning("[%s] 延迟埋点写入失败（已忽略，不影响闸门）：%s", PLUGIN_NAME, exc)
            return False
        self._lines = (self._lines or 0) + 1
        return True

    # ---- 内部（单测里 monkeypatch _append 就能模拟 I/O 失败） -------------
    def _prepare(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._lines is None:
            self._lines = self._count_lines(path)
        if timing_mod.needs_rotate(self._lines, self._size(path)):
            self._rotate(path)

    def _append(self, path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _rotate(self, path: Path) -> None:
        """超限就清空重写，保留最近的一半。"""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            text = ""
        kept = timing_mod.trim_lines(timing_mod.decode_lines(text))
        path.write_text(timing_mod.dump_lines(kept), encoding="utf-8")
        self._lines = len(kept)

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _count_lines(path: Path) -> int:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return sum(1 for _ in handle)
        except FileNotFoundError:
            return 0


@register(
    PLUGIN_NAME,
    "NS9927",
    "拟人：按作息概率放行消息、被点名分档回复，防两个 bot 互刷烧 token",
    PLUGIN_VERSION,
)
class ReplyGate(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._rng = random.Random()

        self._gate = Gate()
        self._queue = queue_mod.SleepQueue()
        self._batch = batch_mod.BatchPlanner()
        self._segments = schedule.parse_time_weights(schedule.DEFAULT_TIME_WEIGHTS)
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_flush_date = None
        self._warned: set[str] = set()
        # 主动开口：默认关闭（_apply_config 会按 proactive 配置重建）
        self._proactive_cfg = proactive.ProactiveConfig()
        self._proactive_slots: tuple[proactive.TimelineSlot, ...] = ()
        self._proactive_states: dict[str, proactive.DailyState] = {}
        self._proactive_seen: set[str] = set()
        """见过的、且命中主动白名单的会话（umo）。
        平台枚举不出会话，所以裸群号 / QQ 号白名单条目只能靠「收到过该会话的消息」来发现
        （见 core/proactive.candidate_sessions）；完整 umo 条目不需要这个。"""
        self._proactive_task: asyncio.Task | None = None
        self._proactive_retired: list[asyncio.Task] = []
        self._recent: dict[str, deque[str]] = {}
        # 分段回复的时间模型（打字抖动）：默认关闭（_apply_config 重建）
        self._seg_cfg = segmented.SegmentedJitterConfig()
        # 「正在输入」状态：默认关闭（_apply_config 重建；unsupported 记忆也在实例上）
        self._typing = typing_mod.TypingIndicator(logger=logger)
        self._typing_logged = ""
        # 方案 B：后续段的补发任务 + 两次补发都失败时暂存的正文（等 flush 重试）
        self._followup_tasks: set[asyncio.Task] = set()
        self._unsent_tails: dict[str, str] = {}
        # 延迟埋点（debug_timing，默认关；开了才写文件）
        self._timing_enabled = False
        self._timing_sink = TimingSink()
        # 给插件页面看的运行计数（不持久化，重启归零）
        self._stats = {
            "drops": 0,
            "allows": 0,
            "queued": 0,
            "flushed": 0,
            "batched": 0,
            "proactive_sent": 0,
        }

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

        # 延迟埋点：默认关。开启/关闭只是加不加记录，判定行为一个字节都不变
        was_timing = self._timing_enabled
        self._timing_enabled = coerce.as_bool(raw.get("debug_timing"), False)
        if self._timing_enabled and not was_timing:
            self._log_timing_enabled()

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
        # 分段回复的时间模型（先读再打 + 段间抖动）；默认关闭
        self._seg_cfg = segmented.SegmentedJitterConfig.from_raw(raw.get("segmented_jitter"))
        if self._seg_cfg.enable and self._seg_cfg.split_mode == "regex":
            # 存配置时就预编译一次：正则写坏能立刻在日志里看到（只报一次）
            segmented.validate_regex(self._seg_cfg.regex, self._regex_warning)
        # 「正在输入」：默认关闭。重建实例会清掉「该会话不支持」的记忆——配置改过就重试一次，
        # 这是想要的：用户可能正是去开了 NapCat 的开关才回来改配置的。
        self._typing = typing_mod.TypingIndicator(
            typing_mod.TypingIndicatorConfig.from_raw(raw.get("typing_indicator")),
            logger=logger,
        )
        line = self._typing.config.describe()
        if self._typing.config.enable and line != self._typing_logged:
            self._typing_logged = line
            logger.info("[%s] %s", PLUGIN_NAME, line)

        # 被点名的回复策略：活跃度高→立刻回；低→攒一波统一回；0→睡眠排队
        addressed = coerce.as_mapping(raw.get("addressed_reply"))
        self._batch = batch_mod.BatchPlanner(
            enabled=coerce.as_bool(addressed.get("enable"), True),
            window_seconds=max(0.0, coerce.as_float(addressed.get("batch_window_seconds"), 120.0)),
            immediate_threshold=min(
                1.0, max(0.0, coerce.as_float(addressed.get("immediate_threshold"), 0.6))
            ),
            max_messages=max(1, coerce.as_int(addressed.get("batch_max_messages"), 10)),
        )

        sleep_cfg = coerce.as_mapping(raw.get("sleep_queue"))
        self._flush_hour = coerce.as_int(sleep_cfg.get("flush_at_hour"), 8)
        self._queue.max_per_session = max(1, coerce.as_int(sleep_cfg.get("max_per_session"), 5))

        # 主动开口：★安全红线——enable=false 或 session_list 为空时**零副作用**：
        # 不解析 timeline、不起循环、不调 provider。所以时间线只在 active 时才解析。
        self._proactive_cfg = proactive.ProactiveConfig.from_raw(raw.get("proactive"))
        self._proactive_slots = ()
        if self._proactive_cfg.active:
            try:
                self._proactive_slots = tuple(
                    proactive.parse_timeline(self._proactive_cfg.timeline)
                )
            except proactive.TimelineError as exc:
                logger.error(
                    "[%s] proactive.timeline 配置有误，回退默认时间线：%s", PLUGIN_NAME, exc
                )
                self._proactive_slots = tuple(proactive.parse_timeline(proactive.DEFAULT_TIMELINE))
        # 配置改完立刻对齐循环：打开就起（幂等），关掉就停
        self._sync_proactive_loop()

    def stats_snapshot(self) -> dict:
        """给插件页面读的运行计数。

        **刻意不加埋点字段**：页面结构是另一处的事，埋点只落 jsonl + 一条 info 日志。
        """
        return dict(self._stats)

    # ------------------------------------------------------------------
    # 延迟埋点（debug_timing；所有记录都不参与判定，只观察）
    # ------------------------------------------------------------------
    def _log_timing_enabled(self) -> None:
        sink = getattr(self, "_timing_sink", None)
        try:
            where = str(sink.path()) if sink is not None else "（数据目录未解析）"
        except Exception:
            where = "（AstrBot 数据目录解析失败，写入时会重试）"
        logger.info("[%s] debug_timing 已开启：每条消息的时序写入 %s", PLUGIN_NAME, where)

    def _build_timing_record(
        self, decision, *, info, event, umo, sender_id, kind, activity, t_wall, t_gate
    ):
        """构造一条时序记录（字段全集，取不到的留 None）。只在埋点开启时调用。"""
        try:
            return timing_mod.normalize_record(
                {
                    "t_recv": info.sent_at or None,
                    "t_recv_wall": t_wall or None,
                    "t_gate": t_gate,
                    "umo": umo,
                    "sender_id": sender_id,
                    "kind": kind.value,
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "prob": round(float(decision.probability), 4),
                    "activity": round(float(activity), 4),
                    "message_id": self._message_id(event),
                    "quoted_id": self._quoted_id(event),
                }
            )
        except Exception as exc:  # 记录构造失败也不能影响闸门
            logger.warning("[%s] 延迟埋点构造失败（已忽略）：%s", PLUGIN_NAME, exc)
            return None

    @staticmethod
    def _message_id(event) -> str:
        """平台消息 id；拿不到就空串。"""
        try:
            value = getattr(getattr(event, "message_obj", None), "message_id", "")
        except Exception:
            return ""
        return "" if value in (None, "") else str(value)

    @staticmethod
    def _quoted_id(event) -> str:
        """消息链里 Reply 组件引用的消息 id；没有引用 / 拿不到就空串。"""
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None)
        except Exception:
            return ""
        return timing_mod.quoted_id_from_message_chain(chain)

    def _stash_timing(self, event, record) -> None:
        """把闸门阶段的记录挂到事件上，等装饰阶段补齐 t_decorate/delay/t_ready。

        优先用 AstrBot 的事件 extras（同一 event 对象贯穿流水线），失败退化成私有属性。
        挂不上就算了：埋点丢一行无所谓，绝不能因此抛异常。
        """
        if record is None:
            return
        try:
            event.set_extra(TIMING_EXTRA_KEY, record)
            return
        except Exception:
            pass
        try:
            setattr(event, TIMING_EXTRA_KEY, record)
        except Exception:
            pass

    def _pop_timing(self, event):
        """取走事件上挂的记录（取过就清掉，避免重复落盘）。"""
        if not self._timing_enabled:
            return None
        try:
            record = event.get_extra(TIMING_EXTRA_KEY, None)
            if record is not None:
                event.set_extra(TIMING_EXTRA_KEY, None)
                return record
        except Exception:
            pass
        record = getattr(event, TIMING_EXTRA_KEY, None)
        if record is not None:
            try:
                setattr(event, TIMING_EXTRA_KEY, None)
            except Exception:
                pass
        return record

    def _write_timing(self, record) -> None:
        """落盘一行。sink.write 自己吞异常，这里再包一层保险。"""
        if record is None or not self._timing_enabled:
            return
        try:
            self._timing_sink.write(record)
        except Exception as exc:  # TimingSink 理论上不抛，双保险
            logger.warning("[%s] 延迟埋点写入异常（已忽略）：%s", PLUGIN_NAME, exc)

    def _finish_timing(self, record, *, delay=None, ready=None) -> None:
        """补齐发送阶段的时间并落盘；没走到发送就留 null（t_decorate 也是 null）。"""
        if record is None:
            return
        record["delay"] = delay
        record["t_ready"] = ready
        self._write_timing(record)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        self._start_flush_loop()
        self._start_proactive_loop()  # 默认配置下 active=False，这里什么都不会做
        try:
            from .webapi import WebApi

            self._web_api = WebApi(self, version=PLUGIN_VERSION)
            count = self._web_api.register()
            logger.info("[%s] 已注册 %d 个插件页面接口（/life/page/*）", PLUGIN_NAME, count)
        except Exception:
            logger.exception("[%s] 插件页面接口注册失败（不影响闸门本身）", PLUGIN_NAME)
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
        flush, self._task = self._task, None
        proactive_task, self._proactive_task = self._proactive_task, None
        retired, self._proactive_retired = self._proactive_retired, []
        followups, self._followup_tasks = list(self._followup_tasks), set()
        for task in (flush, proactive_task, *retired, *followups):
            if task and not task.done():
                task.cancel()
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # 后台任务里的异常只记日志，绝不让卸载失败
                logger.exception("[%s] 后台循环收尾异常（已忽略）", PLUGIN_NAME)
        if self._unsent_tails:
            logger.warning(
                "[%s] 卸载时还有 %d 个会话的正文没补发成功（已放弃）", PLUGIN_NAME, len(self._unsent_tails)
            )
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
                await self._flush_unsent_tails()  # 暂存正文的重试（方案 B 兜底）
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
        self._stats["flushed"] += len(pending)
        for umo, messages in grouped.items():
            text = queue_mod.compose_flush_text(messages)
            try:
                await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
            except Exception:
                logger.exception("[%s] 补发失败 session=%s", PLUGIN_NAME, umo)

    # ------------------------------------------------------------------
    # 主动开口：后台检查循环（照 _flush_loop 的写法）
    # ------------------------------------------------------------------
    def _sync_proactive_loop(self) -> None:
        """配置变更后对齐主动循环：active → 起（幂等）；不 active → 停。"""
        if self._proactive_cfg.active:
            self._start_proactive_loop()
        else:
            self._stop_proactive_loop()

    def _start_proactive_loop(self) -> None:
        """幂等启动主动检查循环。

        ★安全红线：``enable=false`` 或 ``session_list`` 为空 → **直接返回**，
        不创建任务、不解析时间线、不碰 provider（零副作用）。
        """
        if not self._proactive_cfg.active:
            return
        if self._proactive_task and not self._proactive_task.done():
            return
        try:
            self._running = True
            self._proactive_task = asyncio.get_running_loop().create_task(
                self._proactive_loop(), name=f"{PLUGIN_NAME}-proactive"
            )
        except RuntimeError:
            # 没有正在运行的事件循环（比如 __init__ 阶段）：不起任务，等 initialize()
            self._proactive_task = None

    def _stop_proactive_loop(self) -> None:
        """停循环。同步上下文里不能 await，把取消掉的 task 记下来，terminate() 时收尾。"""
        task, self._proactive_task = self._proactive_task, None
        if task and not task.done():
            task.cancel()
            self._proactive_retired = [t for t in self._proactive_retired if not t.done()]
            self._proactive_retired.append(task)

    async def _proactive_loop(self) -> None:
        """每 ``check_interval_seconds`` 醒一次，逐会话跑 should_speak。

        照 ``_flush_loop``：``CancelledError`` 必须重抛（否则 terminate() 的 await 会挂住），
        其余异常吞掉 + ``logger.exception`` + 睡 60 秒继续——**绝不让后台循环死掉**。
        """
        while self._running:
            try:
                # 每轮重读配置：页面上改检查频率立即生效
                interval = max(
                    PROACTIVE_MIN_INTERVAL_SECONDS,
                    float(self._proactive_cfg.check_interval_seconds),
                )
                await asyncio.sleep(interval)
                if not self._running:
                    break
                await self._maybe_proactive_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] 主动开口循环异常，60s 后重试", PLUGIN_NAME)
                await asyncio.sleep(60)

    async def _maybe_proactive_check(self) -> None:
        """一次主动检查：先过一次总闸门（零副作用），再逐会话顺序判定。"""
        cfg = self._proactive_cfg
        if not cfg.active:
            # ★安全红线：关闭 / 无白名单 → 连时间线都不解析，直接返回
            return

        now = datetime.now()
        activity = schedule.activity_ratio(
            now,
            self._segments,
            interpolate_minutes=self._interpolate_minutes,
            weekend_multiplier=self._weekend_multiplier,
            weekend_sleep_shift_hours=self._weekend_shift,
        )
        # 作息表主权：睡着就绝不主动（判定链里也会再确认一次）
        asleep = schedule.sleeping(now, self._segments, weekend_sleep_shift_hours=self._weekend_shift)

        for umo in proactive.candidate_sessions(cfg.session_list, self._proactive_seen):
            try:
                await self._proactive_for_session(umo, now, activity, asleep)
            except Exception:
                # 单个会话出问题不许影响别的会话，更不许把循环搞死
                logger.exception("[%s] 主动开口检查异常 session=%s（已跳过）", PLUGIN_NAME, umo)

    async def _proactive_for_session(
        self, umo: str, now: datetime, activity: float, asleep: bool
    ) -> None:
        """单个会话：判定 → 生成 → 发送 → 记账。"""
        cfg = self._proactive_cfg
        state = self._proactive_states.get(umo)
        if state is None:
            state = proactive.DailyState(umo=umo)
            self._proactive_states[umo] = state

        decision = proactive.should_speak(
            now,
            state,
            cfg,
            self._rng,
            timeline=self._proactive_slots,
            activity=activity,
            sleeping=asleep,
        )
        self._log_proactive(umo, state, cfg, decision)
        if not decision.allow:
            return

        # ★同一次检查里最多一条在飞的 LLM 调用：这里是顺序 await，不存在并发打 provider
        text = await self._generate_proactive_text(umo, cfg)
        if not text:
            # provider 拿不到 / 失败 / 超时 / 空回复：跳过就完了，
            # **绝不在这里重试、绝不阻塞**（下一轮检查自然再判）
            return

        if await self._send_proactive(umo, text, cfg):
            state.note_sent(now, cfg.cooldown_minutes)
            # 计划时刻加抖动：下一次开口不是「准点 45 分钟后」，而是冷却结束
            # ±jitter 分钟内的某个点，且必须落在可打扰窗口里（真人不会准点）。
            planned = proactive.next_window(
                datetime.fromtimestamp(state.cooldown_until),
                cfg,
                self._rng,
                timeline=self._proactive_slots,
            )
            if planned is not None:
                state.cooldown_until = max(state.cooldown_until, planned.timestamp())
            self._stats["proactive_sent"] += 1
            logger.info(
                "[%s] 主动开口已发送 %s | %s | 今日已发 %d/%d",
                PLUGIN_NAME,
                umo,
                decision.log_line(),
                state.sent,
                cfg.daily_budget,
            )

    def _log_proactive(self, umo: str, state: proactive.DailyState, cfg, decision) -> None:
        """每条主动决策都留痕：umo / 档位 / 概率 / reason / 今日已发几条。"""
        line = "[%s] 主动决策 %s | %s | 今日已发 %d/%d 未回复 %d"
        args = (
            PLUGIN_NAME,
            umo,
            decision.log_line(),
            state.sent,
            cfg.daily_budget,
            state.unanswered,
        )
        if self._verbose:
            logger.info(line, *args)
        else:
            logger.debug(line, *args)

    async def _generate_proactive_text(self, umo: str, cfg) -> str:
        """调 provider 生成一条主动内容。失败一律返回空串（调用方跳过）。"""
        try:
            provider = self.context.get_using_provider(umo)
        except Exception:
            logger.exception("[%s] 主动开口取 provider 失败 session=%s", PLUGIN_NAME, umo)
            return ""
        if provider is None:
            self._warn_once(
                "proactive_no_provider",
                "主动开口拿不到 LLM provider，已跳过（不会重试循环，等下次检查）。",
            )
            return ""

        prompt, system_prompt = proactive.compose_prompt(cfg)
        try:
            # ★必须有超时：provider 卡住时绝不能把后台循环挂在那儿。
            #   真机教训见 docs/真机反馈与根因分析.md 第十节（livingmemory 每条 60 秒）。
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    # 空的人设提示传 None，让 AstrBot 用它自己的默认人设
                    system_prompt=system_prompt or None,
                    contexts=self._recent_contexts(umo),
                ),
                timeout=cfg.llm_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] 主动开口 LLM 超时（%.1fs），本条放弃 session=%s",
                PLUGIN_NAME,
                cfg.llm_timeout_seconds,
                umo,
            )
            return ""
        except Exception:
            logger.exception("[%s] 主动开口 LLM 调用失败，本条放弃 session=%s", PLUGIN_NAME, umo)
            return ""
        return proactive.extract_text(response)

    async def _send_proactive(self, umo: str, text: str, cfg) -> bool:
        """分段发送。返回是否**至少发出去一段**（失败只记日志，不抛给循环）。"""
        cleaned = (text or "").strip()
        if len(cleaned) < PROACTIVE_MIN_CHARS:
            logger.info(
                "[%s] 主动开口内容过短已丢弃 session=%s（%d 字）", PLUGIN_NAME, umo, len(cleaned)
            )
            return False

        parts = proactive.split_message(
            cleaned,
            max_chars=PROACTIVE_SEGMENT_MAX_CHARS,
            max_parts=PROACTIVE_SEGMENT_MAX_PARTS,
        )
        if not parts:
            logger.info("[%s] 主动开口内容为空已丢弃 session=%s", PLUGIN_NAME, umo)
            return False

        sent_any = False
        for index, part in enumerate(parts):
            if index:
                await asyncio.sleep(PROACTIVE_SEGMENT_DELAY_SECONDS)  # 段间停一下，别一口气糊上去
            try:
                await self.context.send_message(umo, MessageChain(chain=[Plain(part)]))
            except Exception:
                # 已经发出去的段无法撤回；至少要把预算记上，避免下一轮重复发
                logger.exception("[%s] 主动开口发送失败 session=%s", PLUGIN_NAME, umo)
                break
            sent_any = True
        return sent_any

    # ---- 最近聊天（只给主动开口当参考，关闭时零副作用） ----------------
    def _note_recent(self, umo: str, text: str) -> None:
        """记一条最近的用户消息。只在 proactive active 时被调用。"""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        bucket = self._recent.get(umo)
        if bucket is None:
            bucket = deque(maxlen=PROACTIVE_RECENT_MAX)
            self._recent[umo] = bucket
        bucket.append(cleaned)

    def _recent_contexts(self, umo: str) -> list[dict]:
        """最近聊天转成 provider.text_chat 的 contexts 格式（没有就空列表）。"""
        bucket = self._recent.get(umo)
        if not bucket:
            return []
        return [{"role": "user", "content": text} for text in bucket]

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

        # 收到/开始处理这条事件的时间（和平台发出时间相减 = 平台侧积压）
        t_wall = time.time() if self._timing_enabled else 0.0

        umo = event.unified_msg_origin
        if self._session_whitelist and umo not in self._session_whitelist:
            return

        sender_id = event.get_sender_id()
        if classify.is_self_message(sender_id, event.get_self_id()):
            return

        # 主动开口：只在启用时记录（关闭时零副作用）。
        # - _proactive_seen：裸群号 / QQ 号白名单条目要靠它才找得到真实会话
        # - _note_recent：主动开口「参考最近的聊天」
        # 放在闸门判定之前：被已读不回的消息同样是「最近聊过什么」的一部分。
        if self._proactive_cfg.active and proactive.match_session(
            umo, self._proactive_cfg.session_list
        ):
            self._proactive_seen.add(umo)
            self._note_recent(umo, event.get_message_str())

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
            sent_at=self._message_sent_at(event),
        )
        decision = self._gate.evaluate(info, activity=activity, sleeping=asleep)

        # 判定完成的时间 + 本条的埋点记录（默认关闭时 t_gate 都不取，零副作用）
        timing_rec = None
        if self._timing_enabled:
            timing_rec = self._build_timing_record(
                decision,
                info=info,
                event=event,
                umo=umo,
                sender_id=sender_id,
                kind=kind,
                activity=activity,
                t_wall=t_wall,
                t_gate=time.time(),
            )

        if decision.action is Action.DROP:
            self._stats["drops"] += 1
            event.stop_event()
            self._log_decision(decision, event, now)
            self._write_timing(timing_rec)  # DROP 没有 t_allow，其余字段照记
            return

        # 放行 / 入队 / 攒批：这一瞬间就是 t_allow（DROP 走不到这里）
        if timing_rec is not None:
            timing_rec["t_allow"] = time.time()

        if decision.action is Action.QUEUE:
            self._stats["queued"] += 1
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
            self._write_timing(timing_rec)  # 入队后不会走到发送，就地落盘
            return

        # 被点名：按活跃度分三档（睡眠那档已经在上面 return 了）
        if kind is MessageKind.ADDRESSED:
            plan = self._batch.decide(umo, info.text, activity, info.timestamp or time.time())
            if plan.is_fold:
                self._stats["batched"] += 1
                event.stop_event()
                logger.info(
                    "[%s] 攒着 %s | 活跃度 %.2f 太低，本条并入待回批次（第 %d 条）| %s",
                    PLUGIN_NAME,
                    umo,
                    activity,
                    plan.buffered,
                    info.text[:40],
                )
                if timing_rec is not None:
                    timing_rec["batch"] = {
                        "lead": False,
                        "wait": 0.0,
                        "merged": plan.buffered,
                    }
                self._write_timing(timing_rec)  # 并进批次后本条不会发出去
                return
            if plan.is_lead and plan.wait_seconds > 0:
                logger.info(
                    "[%s] 先攒后回 %s | 活跃度 %.2f < 阈值 %.2f，等 %.0f 秒统一回 | %s",
                    PLUGIN_NAME,
                    umo,
                    activity,
                    self._batch.immediate_threshold,
                    plan.wait_seconds,
                    info.text[:40],
                )
                await asyncio.sleep(plan.wait_seconds)
                if timing_rec is not None:
                    # 等待期间可能又有消息并进来，真正合并条数在 finish 之前才准
                    timing_rec["batch"] = {
                        "lead": True,
                        "wait": plan.wait_seconds,
                        "merged": self._batch.buffered(umo),
                    }
                merged = self._batch.finish(umo)
                if merged:
                    event.message_str = merged  # 让 LLM 一次看到攒下的全部内容
                logger.info(
                    "[%s] 统一回复 %s | 攒了 %d 条，合并后 %d 字",
                    PLUGIN_NAME,
                    umo,
                    plan.buffered if merged else 0,
                    len(merged),
                )

        # 放行：群聊插话这一路必须把内置概率顶开，否则会被二次掷骰子（概率被平方）
        self._stats["allows"] += 1
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

        # 还要过 LLM 和装饰阶段：记录挂在事件上，等 on_decorating_result 补齐再落盘
        self._stash_timing(event, timing_rec)

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

    @staticmethod
    def _message_sent_at(event: AstrMessageEvent) -> float:
        """消息在平台上**发出**的时间（epoch 秒）。拿不到就返回 0 = 不做时效判定。

        回退链（从上到下试，第一个能转成正数的就用）：

        1. 原始事件里的 ``time`` —— **平台发出时间**，这才是我们要的。
           OneBot v11 消息事件自带 ``time``（秒），aiocqhttp 把它挂在
           ``message_obj.raw_message``（``aiocqhttp.Event`` 是 dict 子类，
           ``event["time"]`` / ``event.get("time")`` 都行）。
        2. ``event.message_obj.timestamp`` —— **AstrBot 收到/转换完这条消息的时间**，只做兜底。
           实测（AstrBot 4.28.2，容器内已核对）：``AstrBotMessage.__init__`` 就写了
           ``self.timestamp = int(time.time())``（astrbot_message.py:67-68），aiocqhttp 适配器
           转换完还会再覆写一次（aiocqhttp_platform_adapter.py:421），所以它**永远有值**
           —— 正因为如此，绝不能把它放第一位：那样第 1 条永远被挡住，我们量到的永远是
           「AstrBot 收下之后过了多久」。
        3. 都拿不到 → 0.0：不做时效判定，绝不误杀真人。

        这两个来源对应两种完全不同的病（2026-10-07 真机教训）：
        - 取**平台发出时间**：``age`` = 这条消息从发出到现在有多旧，平台 / NapCat 侧积压和
          AstrBot 内部排队都算得进去 —— 时效闸门要的就是它。
        - 取**接收时间**：``age`` 只等于「AstrBot 收下之后过了多久」，消息在到达我们之前的
          那一段滞后整段消失；而接收时间是它刚进流水线的那一刻，``age`` 在处理前就很小，
          于是闸门**几乎恒不触发**（点名的旧消息照样被回答）。这就是要避免的坑。

        也不能用裸 ``time.time()`` 顶替：那是「我们才处理到它」的时间。
        """
        message_obj = getattr(event, "message_obj", None)
        raw_event = getattr(message_obj, "raw_message", None)
        for raw in (
            ReplyGate._raw_event_field(raw_event, "time"),  # 平台发出时间（优先）
            getattr(message_obj, "timestamp", 0),  # AstrBot 接收时间（兜底）
        ):
            value = ReplyGate._as_epoch_seconds(raw)
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def _raw_event_field(raw_event, key: str):
        """从原始平台事件里取字段。OneBot 的 Event 是 dict 子类，dict / 对象都兜住。"""
        if raw_event is None:
            return 0
        try:
            getter = getattr(raw_event, "get", None)
            if callable(getter):
                return getter(key, 0)
            return getattr(raw_event, key, 0)
        except Exception:
            return 0

    @staticmethod
    def _as_epoch_seconds(raw) -> float:
        """转 epoch 秒。转不了 → 0.0；毫秒（> 1e11）统一换算成秒。"""
        try:
            value = float(raw or 0)
        except (TypeError, ValueError):
            return 0.0
        if value > 1e11:  # 有些适配器给的是毫秒
            value /= 1000.0
        return value

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
        """按作息给回复加延迟，替代固定秒回；开了分段抖动就走「先读再打」模型。

        只做延迟和分段发送，不碰 @ 和引用的插入——那是 astrbot_plugin_at_inline 的职责。

        顺序（同一个钩子里，**先延迟、后分段**）：

        1. ``segmented_jitter.enable`` → 用打字模型算 pre_delay + 段间间隔，
           前 N-1 段自己 ``context.send_message`` 发掉，最后一段留在 result 里；
           **此时跳过扁平的 reply_delay**（打字模型已经涵盖「不是秒回」，避免等两次）。
        2. 打字模型没生效（没开 / 结果不是纯文本 / 空内容）→ 走原来的 reply_delay。

        安全红线：``enable=false`` 时**什么都不做**（不读配置、不切分、不 sleep）；
        ``CancelledError`` 必须 ``raise``；任何异常 ``logger.exception`` 后保证
        最后一段仍由流水线发出（绝不吞回复，也绝不重复发正文）。
        """
        # 闸门阶段挂上来的埋点记录（默认关闭时是 None，零副作用）
        pending = self._pop_timing(event)
        entered = time.time() if (pending is not None or self._timing_enabled) else 0.0

        if not self._enable:
            self._finish_timing(pending)  # 处理过但没发送：t_decorate 留 null
            return
        if event.is_stopped() or event.get_result() is None:
            self._finish_timing(pending)
            return
        umo = event.unified_msg_origin
        if self._session_whitelist and umo not in self._session_whitelist:
            self._finish_timing(pending)
            return

        # 真的要走发送，才记「进入 on_decorating_result 的时间」
        if pending is not None:
            pending["t_decorate"] = entered

        # 延迟是可选的，但会话冷却的计时不受它开关影响：
        # 关了延迟也得记「刚回过一条」，否则冷却永远不触发。
        delay = 0.0
        used_typing_model = False
        # 「正在输入」的 target：私聊用对端 QQ（set_input_status 的 user_id）
        target = typing_mod.TypingTarget(
            umo=umo,
            is_private=bool(event.is_private_chat()),
            user_id=str(event.get_sender_id() or ""),
        )

        if self._seg_cfg.enable:
            try:
                used_typing_model, waited = await self._apply_typing_model(event, target)
                delay += waited
            except asyncio.CancelledError:
                raise  # 不要吞，否则 terminate()/停机流程会挂住
            except Exception:
                # ★绝不因为抖动把回复吞掉：这里只记日志，让结果照常被发出去
                logger.exception("[%s] 分段抖动异常，本条放行原样发送", PLUGIN_NAME)
                used_typing_model = False

        if not used_typing_model and self._delay_cfg.enable:
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
                # 等的时候同时把「正在输入」续起来（真的要发消息才会走到这里）
                async with self._typing.indicator(target, self._typing_sender(event)):
                    await asyncio.sleep(seconds)
                delay += seconds  # 实际睡了多少秒（0 = 没睡）

        # 真的发出去一条，才计入会话冷却
        self._gate.record_reply(umo)

        # 延时结束、准备交给 AstrBot 发送：这条时序到这里就齐了，落盘
        self._finish_timing(pending, delay=delay, ready=time.time())

    # ------------------------------------------------------------------
    # 分段回复的时间模型（打字抖动）
    # ------------------------------------------------------------------
    async def _apply_typing_model(
        self, event: AstrMessageEvent, target: typing_mod.TypingTarget
    ) -> tuple[bool, float]:
        """在钩子里实现「先读再打 + 段间抖动」。

        返回 ``(是否接管, 实际等待秒数)``；没接管（结果不是纯文本 / 切不出多段 /
        空内容）时返回 ``(False, 0.0)``，由调用方退回扁平的 reply_delay。

        **不重不漏的保证**（这是本功能最容易出错的地方）：

        - 结果链**全是 Plain** 才动手（有图片 / at 等非文本段 → 整条不切，保守）；
        - 先按字数算好 ``TimingPlan`` 并打日志，**然后立刻把结果链的文本改成「最后一段」**，
          再去 sleep + 发送前面的段。也就是说：**改写只发生一次，且在任何 sleep/send 之前**。
        - 前 N-1 段我们自己 ``context.send_message`` 发；最后一段**只**存在于 result 里，
          由流水线在钩子返回后正常发出。
        - 中途发失败：只记日志并停止发送剩余段，**result 里已经是最后一段**，
          流水线照发 → 已发出去的段不会被重复，正文也不会整条重发（绝不会出现
          「前几段发过 + 完整正文又发一遍」）。
        """
        result = event.get_result()
        chain = getattr(result, "chain", None)
        if not chain or not all(isinstance(comp, Plain) for comp in chain):
            # 有非文本段（图片/at/转发…）：整条不切，交给扁平延迟
            return False, 0.0

        text = "".join(getattr(comp, "text", "") or "" for comp in chain)
        if not text.strip():
            return False, 0.0

        # ★零副作用：enable=false 时上面的调用方根本不会进来（这个函数里也不读配置之外的东西）
        segments = segmented.split_text(text, self._seg_cfg, on_error=self._regex_warning)
        if not segments:
            return False, 0.0

        incoming_chars = self._incoming_chars(event) if self._seg_cfg.count_incoming_chars else 0
        plan = segmented.plan_timing(incoming_chars, segments, self._seg_cfg, self._rng)

        # 真机排障靠这一行：用户拿它调 typing_chars_per_second / reading_chars_per_second
        logger.info(
            "[%s] 打字模型 %s | 切成 %d 段 | %s%s",
            PLUGIN_NAME,
            target.umo,
            len(segments),
            plan.log_line(),
            "" if len(segments) > 1 else "（单段，只走 pre_delay 不分段）",
        )
        if self._delay_cfg.enable:
            logger.info(
                "[%s] 本次用打字模型，跳过 reply_delay（避免等两次）", PLUGIN_NAME
            )

        # 等待期间并发续「正在输入」；只在**第一条发出之前**续——真人打完字就发出去了，
        # 第一条之后不该还显示正在输入。所以这里只包住 pre_delay。
        waited = 0.0
        if plan.pre_delay > 0:
            sender = self._typing_sender(event)
            async with self._typing.indicator(target, sender):
                await asyncio.sleep(plan.pre_delay)
            waited += plan.pre_delay

        if len(segments) == 1:
            # 单段：不动 chain、不注册后续段；只贡献 pre_delay
            # （长回复的打字时间仍然生效，否则「长回复更久」对 D 这种无标点长句就失效了）
            return True, waited

        # ★方案 B 的改写点：只把**第一段**留在 result 里，交给流水线给它加引用/@ 再发出。
        #   其余段由 after_message_sent 唤醒的后台任务补发（见 _schedule_followups）。
        first, tail = segments[0], tuple(segments[1:])
        chain[:] = [Plain(first)]
        if tail:
            self._schedule_followups(event, target.umo, tail, plan.segment_delays)
        return True, waited

    # ---- 方案 B：第一段之后补发其余段 ----------------------------------
    def _schedule_followups(
        self,
        event: AstrMessageEvent,
        umo: str,
        tail: tuple[str, ...],
        delays: tuple[float, ...],
    ) -> None:
        """登记后续段，并起一个等 ``after_message_sent`` 的后台任务。"""
        record = _FollowUp(
            umo=umo,
            tail=tuple(tail),
            delays=tuple(delays),
            event=event,
            signal=asyncio.Event(),
        )
        self._stash_followup(event, record)
        try:
            task = asyncio.get_running_loop().create_task(
                self._deliver_followups(record), name=f"{PLUGIN_NAME}-followup"
            )
        except RuntimeError:
            # 理论上到不了（本方法在协程里）：别留一个没人等的记录
            logger.warning(
                "[%s] 拿不到事件循环，后续 %d 段无法补发", PLUGIN_NAME, len(record.tail)
            )
            self._pop_followup(event)
            return
        self._followup_tasks.add(task)
        task.add_done_callback(self._followup_tasks.discard)

    async def _deliver_followups(self, record: _FollowUp) -> int:
        """等第一条真正发出，再按抖动间隔补发 ``record.tail``。返回成功发出的段数。

        超时兜底：``after_message_sent`` 没触发时，**先确认第一条真的出去了**
        （respond 阶段跑完会 ``event.clear_result()``）才补发——
        否则事件被停掉时会发出「没有第一段」的孤儿后续段。
        """
        try:
            try:
                await asyncio.wait_for(
                    record.signal.wait(), timeout=FOLLOWUP_HOOK_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                if self._first_segment_still_pending(record):
                    logger.warning(
                        "[%s] after_message_sent 未触发且第一条似乎没发出去，"
                        "后续 %d 段不补发（避免孤儿段）session=%s",
                        PLUGIN_NAME,
                        len(record.tail),
                        record.umo,
                    )
                    return 0
                logger.warning(
                    "[%s] after_message_sent 未触发，改用兜底发送后续 %d 段 session=%s",
                    PLUGIN_NAME,
                    len(record.tail),
                    record.umo,
                )
            return await self._send_followup_segments(record)
        except asyncio.CancelledError:
            raise  # 卸载时取消：不要吞
        except Exception:
            logger.exception("[%s] 后续分段发送异常 session=%s", PLUGIN_NAME, record.umo)
            return 0

    @staticmethod
    def _first_segment_still_pending(record: _FollowUp) -> bool:
        """第一条是否还没被流水线发出去（结果还挂在 event 上 = respond 阶段没跑完）。"""
        try:
            getter = getattr(record.event, "get_result", None)
            return callable(getter) and getter() is not None
        except Exception:
            return False  # 拿不准就别拦着补发

    async def _send_followup_segments(self, record: _FollowUp) -> int:
        """按计划逐段补发 tail。返回**已成功发出去的段数**。

        失败时**绝不丢正文**：把剩下的段合并成一条补发（已发出的段不重发）。
        """
        sent = 0
        for index, part in enumerate(record.tail):
            gap = record.delays[index] if index < len(record.delays) else 0.0
            if gap > 0:
                await asyncio.sleep(gap)  # 基准 = 第一条真正发出的时刻（钩子触发点）
            try:
                await self.context.send_message(
                    record.umo, MessageChain(chain=[Plain(part)])
                )
            except Exception:
                remaining = "".join(record.tail[index:])
                logger.exception(
                    "[%s] 后续分段发送失败，把剩余 %d 段合并补发 session=%s（已发 %d 段不重发）",
                    PLUGIN_NAME,
                    len(record.tail) - index,
                    record.umo,
                    sent,
                )
                await self._resend_remainder(record.umo, remaining)
                return sent
            sent += 1
        return sent

    async def _resend_remainder(self, umo: str, text: str) -> bool:
        """把没发出去的正文合并成一条补发；连这条都失败就暂存，等 flush 再试。"""
        if not text:
            return True
        try:
            await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
            return True
        except Exception:
            self._unsent_tails[umo] = self._unsent_tails.get(umo, "") + text
            logger.exception(
                "[%s] 合并补发也失败，剩余正文已暂存（下一次 flush 重试）session=%s",
                PLUGIN_NAME,
                umo,
            )
            return False

    async def _flush_unsent_tails(self) -> None:
        """flush 循环每轮重试暂存的正文——**不让正文永久消失**。"""
        if not self._unsent_tails:
            return
        pending, self._unsent_tails = self._unsent_tails, {}
        for umo, text in pending.items():
            try:
                await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
                logger.info(
                    "[%s] 暂存正文补发成功 session=%s（%d 字）", PLUGIN_NAME, umo, len(text)
                )
            except Exception:
                self._unsent_tails[umo] = self._unsent_tails.get(umo, "") + text
                logger.exception("[%s] 暂存正文补发仍失败，继续留着 session=%s", PLUGIN_NAME, umo)

    def _stash_followup(self, event, record: _FollowUp) -> None:
        """把后续段记录挂在 event 上（优先 extras，退化到私有属性）。"""
        try:
            event.set_extra(FOLLOWUP_EXTRA_KEY, record)
            return
        except Exception:
            pass
        try:
            setattr(event, FOLLOWUP_EXTRA_KEY, record)
        except Exception:
            pass

    @staticmethod
    def _pop_followup(event):
        try:
            record = event.get_extra(FOLLOWUP_EXTRA_KEY, None)
            if record is not None:
                event.set_extra(FOLLOWUP_EXTRA_KEY, None)
                return record
        except Exception:
            pass
        record = getattr(event, FOLLOWUP_EXTRA_KEY, None)
        if record is not None:
            try:
                setattr(event, FOLLOWUP_EXTRA_KEY, None)
            except Exception:
                pass
        return record

    @filter.after_message_sent(priority=GATE_PRIORITY)
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """第一条（带引用/@ 的那条）真的发出去之后，唤醒后续段补发。

        触发点：``core/pipeline/respond/stage.py:328``
        ``if await call_event_hook(event, EventType.OnAfterMessageSentEvent)``
        —— 所有段发完之后、``event.clear_result()`` 之前，handler 拿到的是**同一个 event**
        （``core/pipeline/context_utils.py:95-110`` 用 ``handler.handler(event)`` 调用）。

        ★这里只 ``set()`` 一个事件、**不做发送**：既不阻塞 respond 阶段，也让
        「段间抖动的基准 = 第一条真正发出的时刻」天然成立。
        """
        try:
            record = self._pop_followup(event)
            if record is not None:
                record.signal.set()
        except Exception:
            # 唤醒失败不会丢正文：后台任务等超时后走兜底发送
            logger.exception("[%s] after_message_sent 唤醒失败（后续段改走兜底）", PLUGIN_NAME)

    # ---- 「正在输入」 --------------------------------------------------
    @staticmethod
    def _regex_warning(pattern: str, exc: Exception) -> None:
        """``segmented_jitter.regex`` 编译失败：退回 chars 模式，**只报一次**。"""
        logger.warning(
            "[%s] segmented_jitter.regex 编译失败，已退回 chars 模式切分：%r（%s）",
            PLUGIN_NAME,
            pattern,
            exc,
        )

    def _typing_sender(self, event: AstrMessageEvent):
        """返回一个 sender(target) 协程函数（把 event 绑进去，内核不碰 astrbot 对象）。"""

        async def sender(target: typing_mod.TypingTarget) -> None:
            bot = getattr(event, "bot", None)
            call_action = getattr(bot, "call_action", None)
            if not callable(call_action):
                # 非 aiocqhttp 适配器（或桩环境）：算「该会话不支持」，只记一次 debug
                raise RuntimeError("当前平台没有 bot.call_action，无法发「正在输入」")
            # ★超时在 core 里包（TypingIndicator），这里只管发
            await call_action("set_input_status", user_id=str(target.user_id), event_type=1)

        return sender

    def _incoming_chars(self, event: AstrMessageEvent) -> int:
        """入站消息字数（只算 Plain 段）。取不到就 0 —— 不猜、不报错。

        路径（从上到下试）：
        1. ``event.message_obj.message``（AstrBotMessage.message，Nakuru 消息链）里的 Plain 段；
        2. ``event.message_obj.message_str``（AstrBot 已经抽好的纯文本）兜底；
        3. 都没有 → 0（等于不做「阅读」这一段，只用打字时间）。
        """
        try:
            message_obj = getattr(event, "message_obj", None)
            chain = getattr(message_obj, "message", None)
            if chain:
                total = 0
                for comp in chain:
                    if isinstance(comp, Plain):
                        total += len(getattr(comp, "text", "") or "")
                if total > 0:
                    return total
            text = getattr(message_obj, "message_str", None)
            if isinstance(text, str) and text.strip():
                return len(text.strip())
        except Exception:  # 取字数失败绝不能影响发送
            logger.debug("[%s] 入站字数取不到，按 0 算", PLUGIN_NAME)
        return 0

