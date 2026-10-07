"""概率闸门 + 已读不回。

设计依据：docs/可行性报告与设计.md 第二节（语义）与第八节（三层防护）。

判定链（从便宜到贵、从硬到软，任何一步 DROP 都省下后面全部成本）：

    静默名单命中？        → DROP（永远，连点名也不放）
    睡眠时段？            → 点名/私聊 QUEUE（起床补发），群聊闲聊 DROP
    循环熔断中？          → DROP（点名可豁免，避免真人在 @ 时变哑巴）
    会话冷却中？          → DROP（点名可豁免）
    加权概率放行？        → ALLOW / DROP（计入连续丢弃）
    连续丢弃已达上限？    → 强制 ALLOW（防止「连发三条一条都不回」）

本模块不依赖 AstrBot，也不做 I/O：时间靠注入的 clock，随机数靠注入的 Random，
所以同一串输入必然得到同一串判定，可以直接单测。
"""
from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping

from . import coerce


class MessageKind(str, Enum):
    """消息类型。决定用不用概率——这一条做错整个插件就废了。"""

    ADDRESSED = "addressed"  # 被 @ / 被私聊：真人被点名一定会回，绝不用概率
    CHIME = "chime"  # 群聊插话：群里在聊但没 @ 它
    PROACTIVE = "proactive"  # 主动开口：没人问，自己找话题


class Action(str, Enum):
    ALLOW = "allow"  # 正常走 pipeline（会进 LLM）
    DROP = "drop"  # 已读不回：stop_event，0 token
    QUEUE = "queue"  # 睡眠期点名：入队，起床后补发


# 判定原因，日志里直接用，别写散字符串
REASON_ALLOW = "allow"
REASON_SILENCE_LIST = "silence_list"
REASON_SLEEP_CHIME = "sleep_chime"
REASON_SLEEP_DROPPED = "sleep_dropped"
REASON_SLEEP_QUEUED = "sleep_queued"
REASON_LOOP_BREAKER = "loop_breaker"
REASON_COOLDOWN = "cooldown"
REASON_PROBABILITY = "probability"
REASON_CONSECUTIVE_FLOOR = "consecutive_floor"


@dataclass(frozen=True)
class MessageInfo:
    """待判定消息的最小信息集。由 main.py 从 AstrMessageEvent 里提取。"""

    umo: str  # unified_msg_origin，会话标识
    sender_id: str
    kind: MessageKind
    sender_name: str = ""
    text: str = ""
    timestamp: float = 0.0  # 0 = 用 Gate 的 clock


@dataclass(frozen=True)
class GateDecision:
    action: Action
    reason: str
    probability: float
    activity: float
    kind: MessageKind
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.action is not Action.DROP

    def log_line(self) -> str:
        base = f"[{self.action.value}] {self.reason} kind={self.kind.value} P={self.probability:.3f} 活跃度={self.activity:.2f}"
        return f"{base} {self.detail}" if self.detail else base


@dataclass(frozen=True)
class GateConfig:
    """闸门配置。全部可配，默认值与 _conf_schema.json 一致。"""

    base_probability: Mapping[str, float] = field(
        default_factory=lambda: {"chime": 0.03, "proactive": 0.3, "addressed": 1.0}
    )
    consecutive_drop_limit: int = 3
    consecutive_drop_scope: str = "sender"  # "sender"（默认，推荐）或 "session"
    silence_list: frozenset[str] = frozenset()

    loop_breaker_enable: bool = True
    alternate_rounds: int = 4
    silence_minutes: float = 30.0
    exempt_addressed_from_loop: bool = True

    cooldown_enable: bool = True
    cooldown_window_seconds: float = 60.0
    cooldown_max_replies: int = 3
    exempt_addressed_from_cooldown: bool = True

    sleep_queue_enable: bool = True
    sleep_queue_only_addressed: bool = True

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> GateConfig:
        """从插件配置 dict 构造。缺键用默认值、写坏也用默认值——配置填错不许把插件搞崩。"""

        def sub(key: str) -> Mapping[str, Any]:
            return coerce.as_mapping((raw or {}).get(key))

        base_raw = sub("base_probability")
        base = {
            "chime": coerce.as_float(base_raw.get("chime"), 0.03),
            "proactive": coerce.as_float(base_raw.get("proactive"), 0.3),
            "addressed": coerce.as_float(base_raw.get("addressed"), 1.0),
        }

        loop = sub("loop_breaker")
        queue = sub("sleep_queue")
        cooldown = sub("session_cooldown")  # 设计文档 8.4 第 3 层

        return cls(
            base_probability=base,
            consecutive_drop_limit=max(0, coerce.as_int((raw or {}).get("consecutive_drop_limit"), 3)),
            consecutive_drop_scope=(
                "session"
                if str((raw or {}).get("consecutive_drop_scope", "sender")).strip().lower()
                == "session"
                else "sender"
            ),
            silence_list=coerce.as_id_list((raw or {}).get("silence_list")),
            loop_breaker_enable=coerce.as_bool(loop.get("enable"), True),
            alternate_rounds=max(1, coerce.as_int(loop.get("alternate_rounds"), 4)),
            silence_minutes=coerce.as_float(loop.get("silence_minutes"), 30.0),
            exempt_addressed_from_loop=coerce.as_bool(loop.get("exempt_addressed"), True),
            cooldown_enable=coerce.as_bool(cooldown.get("enable"), True),
            cooldown_window_seconds=coerce.as_float(cooldown.get("window_seconds"), 60.0),
            cooldown_max_replies=max(1, coerce.as_int(cooldown.get("max_replies"), 3)),
            exempt_addressed_from_cooldown=coerce.as_bool(cooldown.get("exempt_addressed"), True),
            sleep_queue_enable=coerce.as_bool(queue.get("enable"), True),
            sleep_queue_only_addressed=coerce.as_bool(queue.get("only_addressed"), True),
        )


@dataclass
class SessionState:
    """单个会话的可变状态。重启即清空——闸门不需要跨重启记忆。"""

    consecutive_drops: int = 0
    """会话级连丢计数，**只用于日志**。"""

    sender_drops: dict[str, int] = field(default_factory=dict)
    """发送者级连丢计数，**连续丢弃兜底看这个**。

    2026-10-07 真机实测后改的：会话级计数会让一个热闹的群变成「每 N 条必插一句」——
    群里 8 个人各说一句、彼此不相关，计数照样累加。
    设计文档的动机是「**对方**连发 3 条一条都不回」，那必须是同一个人。
    """

    muted_until: float = 0.0
    mute_reason: str = ""
    senders: deque[str] = field(default_factory=deque)
    reply_times: deque[float] = field(default_factory=deque)

    def in_mute(self, now: float) -> bool:
        return now < self.muted_until

    def muted_left(self, now: float) -> float:
        return max(0.0, self.muted_until - now)

    def dropped_from(self, sender_id: str) -> int:
        return self.sender_drops.get(sender_id, 0)

    def note_drop(self, sender_id: str) -> None:
        self.consecutive_drops += 1
        if sender_id:
            self.sender_drops[sender_id] = self.sender_drops.get(sender_id, 0) + 1

    def note_answered(self, sender_id: str) -> None:
        self.consecutive_drops = 0
        if sender_id:
            self.sender_drops.pop(sender_id, None)


class Gate:
    """有状态的闸门。会话状态自己维护，调用方只管喂消息。"""

    def __init__(
        self,
        config: GateConfig | None = None,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config or GateConfig()
        self._rng = rng or random.Random()
        self._clock = clock
        self._sessions: dict[str, SessionState] = {}

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def session(self, umo: str) -> SessionState:
        state = self._sessions.get(umo)
        if state is None:
            # 交替检测要看到 2*rounds 条才成立，窗口留够
            state = SessionState(senders=deque(maxlen=max(2, self.config.alternate_rounds * 2)))
            self._sessions[umo] = state
        return state

    def reset_session(self, umo: str) -> None:
        self._sessions.pop(umo, None)

    def record_reply(self, umo: str, timestamp: float | None = None) -> None:
        """真的回出去一条消息时调用（main.py 在回复落地后调）。会话冷却靠它计时。"""
        now = self._clock() if timestamp is None else timestamp
        state = self.session(umo)
        state.reply_times.append(now)
        self._prune_replies(state, now)

    def _prune_replies(self, state: SessionState, now: float) -> None:
        window = self.config.cooldown_window_seconds
        while state.reply_times and now - state.reply_times[0] > window:
            state.reply_times.popleft()

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def evaluate(self, info: MessageInfo, *, activity: float, sleeping: bool) -> GateDecision:
        """跑完整条判定链。

        activity：当前活跃度 0~1（来自 schedule.activity_ratio）
        sleeping：是否睡眠时段（来自 schedule.sleeping，用未插值权重判断）
        """
        cfg = self.config
        now = info.timestamp or self._clock()
        state = self.session(info.umo)
        activity = min(1.0, max(0.0, float(activity)))
        addressed = info.kind is MessageKind.ADDRESSED

        # 交替检测永远先记录：判定结果不影响「谁在跟谁来回」
        self._track_sender(state, info.sender_id, now)

        # 1) 静默名单：硬规则，点名叫也不放
        if info.sender_id and info.sender_id in cfg.silence_list:
            return self._drop(info, state, REASON_SILENCE_LIST, 0.0, activity, "发送者在静默名单")

        # 2) 睡眠时段
        if sleeping:
            if addressed or not cfg.sleep_queue_only_addressed:
                if cfg.sleep_queue_enable:
                    state.note_answered(info.sender_id)
                    return GateDecision(
                        Action.QUEUE,
                        REASON_SLEEP_QUEUED,
                        1.0,
                        activity,
                        info.kind,
                        "睡眠期点名，入队等起床补发",
                    )
                return self._drop(
                    info, state, REASON_SLEEP_DROPPED, 0.0, activity, "睡眠期，且未开排队补发"
                )
            return self._drop(info, state, REASON_SLEEP_CHIME, 0.0, activity, "睡眠期群聊闲聊，直接丢")

        # 3) 循环熔断
        if cfg.loop_breaker_enable and state.in_mute(now):
            if not (addressed and cfg.exempt_addressed_from_loop):
                left = state.muted_left(now)
                return self._drop(
                    info,
                    state,
                    REASON_LOOP_BREAKER,
                    0.0,
                    activity,
                    f"熔断中（{state.mute_reason}），还剩 {left / 60:.1f} 分钟",
                )

        # 4) 会话冷却
        if cfg.cooldown_enable:
            self._prune_replies(state, now)
            if len(state.reply_times) >= cfg.cooldown_max_replies:
                if not (addressed and cfg.exempt_addressed_from_cooldown):
                    return self._drop(
                        info,
                        state,
                        REASON_COOLDOWN,
                        0.0,
                        activity,
                        f"{cfg.cooldown_window_seconds:.0f}s 内已回 {len(state.reply_times)} 条",
                    )

        # 5) 概率
        if addressed:
            # 被点名绝不用概率：真人被 @ 一定会回，随机不回 = 像坏了
            probability = min(1.0, max(0.0, cfg.base_probability.get("addressed", 1.0)))
        else:
            base = cfg.base_probability.get(info.kind.value, 0.0)
            probability = min(1.0, max(0.0, base * activity))

        # 6) 连续丢弃上限：同一个人连着被无视够多次了，这条必回
        if cfg.consecutive_drop_limit:
            if cfg.consecutive_drop_scope == "session":
                dropped = state.consecutive_drops
                who = "本会话"
            else:
                dropped = state.dropped_from(info.sender_id)
                who = "该发送者"
            if dropped >= cfg.consecutive_drop_limit:
                state.note_answered(info.sender_id)
                return GateDecision(
                    Action.ALLOW,
                    REASON_CONSECUTIVE_FLOOR,
                    max(probability, 1.0),
                    activity,
                    info.kind,
                    f"{who}已连续被丢 {dropped} 条，本条强制放行",
                )

        if probability >= 1.0 or self._rng.random() < probability:
            state.note_answered(info.sender_id)
            return GateDecision(Action.ALLOW, REASON_ALLOW, probability, activity, info.kind, "")

        return self._drop(
            info,
            state,
            REASON_PROBABILITY,
            probability,
            activity,
            f"掷骰子没过（{probability:.3f}）",
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _drop(
        self,
        info: MessageInfo,
        state: SessionState,
        reason: str,
        probability: float,
        activity: float,
        detail: str,
    ) -> GateDecision:
        state.note_drop(info.sender_id)
        if self.config.consecutive_drop_scope == "session":
            tail = f"（本会话已连丢 {state.consecutive_drops} 条）"
        else:
            tail = f"（该发送者已连丢 {state.dropped_from(info.sender_id)} 条）"
        return GateDecision(
            Action.DROP, reason, probability, activity, info.kind, f"{detail}{tail}"
        )

    def _track_sender(self, state: SessionState, sender_id: str, now: float) -> None:
        """记录发言者，识别 A→B→A→B 交替循环。"""
        if not sender_id:
            return
        state.senders.append(sender_id)

        if not self.config.loop_breaker_enable:
            return

        rounds = self.config.alternate_rounds
        need = rounds * 2
        if len(state.senders) < need:
            return

        window = list(state.senders)[-need:]
        # 严格交替：相邻两条必须不同，且窗口里只有两个不同的 id
        if any(window[i] == window[i + 1] for i in range(need - 1)):
            return
        if len(set(window)) != 2:
            return

        a, b = window[0], window[1]
        state.muted_until = now + self.config.silence_minutes * 60
        state.mute_reason = f"{a}↔{b} 交替 {rounds} 轮"
        state.senders.clear()  # 清空，避免解除静默后立刻二次触发
