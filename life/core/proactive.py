"""主动开口（PROACTIVE）：一天的时间线 + 可打扰度 → 该不该自己找话题。

**设计灵感来源**：第三方插件 ``FloranceYeh/astrbot_plugin_virtual_life``（GPL-3.0，
与本项目同为 GPL 兼容）。借鉴的是它的**参数体系与判定顺序**——一天时间线的
``availability`` 档位（blocked / low / normal / high）、按档位给概率、每日预算 +
会话冷却 + 未回复上限、计划时刻的 jitter 抖动、长内容分段发送。
**代码为本项目重写**，没有拷贝其实现。

本模块是**纯逻辑**：不 import astrbot、不做 I/O、不自己取时间也不自己抽随机数
（``now`` / ``rng`` / ``activity`` / ``sleeping`` / provider 结果全部从外面注入），
所以同一串输入必然得到同一串判定，可以直接单测。

判定顺序（从硬到软，任何一步拒绝都省下后面全部成本）：

    总开关？          → disabled（★零副作用：不解析时间线、不调 provider）
    有白名单？        → empty_session_list（空 = 不生效，保守）
    在名单里？        → not_in_session_list
    睡眠时段？        → sleeping（复用我们自己的作息表，这是我们的闸门主权）
    档位 + 概率       → blocked_slot / probability（p = 档位概率 × 活跃度）
    未回复超限？      → max_unanswered
    每日预算？        → daily_budget_exhausted
    冷却中？          → cooldown
    空闲不足？        → insufficient_idle

⚠️ **概率只乘一次**：``p = probability[档位] * activity_ratio``。不再乘
``gate.base_probability.proactive``——那等于把概率平方（main.py 的
``_open_active_reply`` 已经踩过这个坑：内置那只手会二次掷骰子）。
gate 的 ``MessageKind.PROACTIVE`` 档位仍然服务「有事件可依附」的那条路；
主动开口没有事件可依附，档位概率表就是这一路的基础概率。

⚠️ ``probability["blocked"]`` 默认 0.0，于是 blocked 档等价于**硬闸门**
（拒绝原因走 ``blocked_slot``）。把这一项调大于 0 才会让「可打扰度=blocked」的
时段重新参与掷骰子——两个旋钮都活着，不写死。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from . import coerce

# ----------------------------------------------------------------------
# 档位（可打扰度）
# ----------------------------------------------------------------------
BLOCKED = "blocked"
LOW = "low"
NORMAL = "normal"
HIGH = "high"

LEVELS: tuple[str, ...] = (BLOCKED, LOW, NORMAL, HIGH)
"""从「请勿打扰」到「随时可以」的四个档位。"""

DEFAULT_TIMELINE = "0-8=blocked,8-10=low,10-12=normal,12-14=normal,14-19=low,19-23=high,23-24=low"
"""默认时间线（与 _conf_schema.json 的 proactive.timeline.default 一致）。"""

DEFAULT_PROBABILITY: dict[str, float] = {BLOCKED: 0.0, LOW: 0.25, NORMAL: 0.7, HIGH: 1.0}
"""vlife 的默认档位概率：blocked 0% / low 25% / normal 70% / high 100%。"""

DEFAULT_PROMPT_TEMPLATE = "（现在没什么事，想主动找 Ta 说句话。可以参考最近的聊天，但不要复述。）"

DEFAULT_LLM_TIMEOUT_SECONDS = 45.0
"""单次 LLM 调用的硬超时。★绝不允许无限等：见 docs/真机反馈与根因分析.md 第十节
（livingmemory 每条回复白吃 60 秒的教训）。"""

DAY_HOURS = 24.0

# 分段发送的默认参数（main.py 里也有同名常量，这里给纯逻辑兜底）
SEGMENT_MAX_CHARS = 120
SEGMENT_MAX_PARTS = 3

_SLOT_RE = re.compile(
    r"^\s*(?P<start>\d{1,2}(?:\.\d+)?)\s*-\s*(?P<end>\d{1,2}(?:\.\d+)?)\s*=\s*(?P<level>[A-Za-z_]+)\s*$"
)
_SENTENCE_RE = re.compile(r"[^。！？!?…\n]*[。！？!?…\n]+|[^。！？!?…\n]+")


class TimelineError(ValueError):
    """proactive.timeline 配置文本解析失败。

    和 schedule.TimeWeightsError 一个态度：**配置填错要吵**，
    由 main.py 捕获后退回默认时间线并打一条 error 日志。
    """


# ----------------------------------------------------------------------
# 拒绝 / 放行原因：日志里直接用，别写散字符串
# ----------------------------------------------------------------------
REASON_ALLOW = "allow"
REASON_DISABLED = "disabled"
REASON_EMPTY_LIST = "empty_session_list"
REASON_NOT_IN_LIST = "not_in_session_list"
REASON_SLEEPING = "sleeping"
REASON_BLOCKED = "blocked_slot"
REASON_PROBABILITY = "probability"
REASON_UNANSWERED = "max_unanswered"
REASON_BUDGET = "daily_budget_exhausted"
REASON_COOLDOWN = "cooldown"
REASON_IDLE = "insufficient_idle"

REJECT_REASONS: tuple[str, ...] = (
    REASON_DISABLED,
    REASON_EMPTY_LIST,
    REASON_NOT_IN_LIST,
    REASON_SLEEPING,
    REASON_BLOCKED,
    REASON_PROBABILITY,
    REASON_UNANSWERED,
    REASON_BUDGET,
    REASON_COOLDOWN,
    REASON_IDLE,
)
"""全部「拒绝」原因。测试用它保证每条都有独立常量、不重不漏。"""


# ----------------------------------------------------------------------
# 时间线
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class TimelineSlot:
    """一段可打扰度时段。start/end 是小时（0~24，允许 end == 24）。"""

    start: float
    end: float
    level: str

    @property
    def duration(self) -> float:
        return self.end - self.start

    def contains(self, hours: float) -> bool:
        return self.start <= hours < self.end


def parse_timeline(text: str) -> list[TimelineSlot]:
    """把配置文本解析成有序、连续、覆盖 [0, 24] 的时段列表。

    格式：``起始小时-结束小时=档位``，用逗号或换行分隔（两种都收）。例如::

        0-8=blocked,8-10=low,10-12=normal,12-14=normal,14-19=low,19-23=high,23-24=low

    - ``#`` 开头或空行忽略
    - ``end < start`` 视为跨零点，自动拆成 [start,24] + [0,end]
    - 档位必须是 blocked / low / normal / high 之一，写错直接抛 TimelineError
    - **没覆盖到的时间一律补成 blocked（请勿打扰）**：漏配的时段宁可哑巴，也不要吵人
    - 时段重叠时**后写的生效**（含完全包含的情形，例如
      ``0-24=blocked,10-12=high`` = 整天别打扰、中午除外）

    解析失败抛 TimelineError（清晰到能直接贴进日志），由调用方决定怎么兜底。
    """
    if not text or not text.strip():
        raise TimelineError("proactive.timeline 为空")

    raw: list[TimelineSlot] = []
    # 逗号（含中文逗号）当分隔符：一行写完和分多行写都行
    normalized = text.replace("，", ",").replace(",", "\n")
    for lineno, line in enumerate(normalized.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _SLOT_RE.match(stripped)
        if not match:
            raise TimelineError(
                f"第 {lineno} 段格式错误：{stripped!r}"
                f"（应为 起始-结束=档位，档位取 {'/'.join(LEVELS)} 之一）"
            )

        start = float(match.group("start"))
        end = float(match.group("end"))
        level = match.group("level").strip().lower()

        if level not in LEVELS:
            raise TimelineError(
                f"第 {lineno} 段档位未知：{level!r}（必须是 {'/'.join(LEVELS)} 之一）"
            )
        if not 0 <= start < DAY_HOURS:
            raise TimelineError(f"第 {lineno} 段起始小时越界：{start}（应在 0~23）")
        if not 0 < end <= DAY_HOURS:
            raise TimelineError(f"第 {lineno} 段结束小时越界：{end}（应在 1~24）")
        if start == end:
            raise TimelineError(f"第 {lineno} 段长度为 0：{stripped!r}")

        if end < start:  # 跨零点
            raw.append(TimelineSlot(start, DAY_HOURS, level))
            raw.append(TimelineSlot(0.0, end, level))
        else:
            raw.append(TimelineSlot(start, end, level))

    if not raw:
        raise TimelineError("proactive.timeline 里没有任何有效时段")

    return _overlay(raw)


def _overlay(slots: list[TimelineSlot]) -> list[TimelineSlot]:
    """把各时段按**书写顺序**盖在「整天 blocked」的底板上。

    为什么不像 schedule._fill_and_sort 那样排序后截断：时间线最常见的写法是
    「整天 blocked + 中午开个口子」（``0-24=blocked,10-12=high``），
    那条口子完全包含在整天里，排序法会把**它**当成被覆盖的一方丢掉。
    这里按顺序覆盖，后写的永远赢，语义简单且符合直觉。

    结果保证：有序、连续、覆盖 [0, 24]，任意时刻都能查到档位。
    """
    out: list[TimelineSlot] = [TimelineSlot(0.0, DAY_HOURS, BLOCKED)]
    for slot in slots:
        _cut_and_insert(out, slot)
    return out


def _cut_and_insert(intervals: list[TimelineSlot], slot: TimelineSlot) -> None:
    """把 slot 盖到 intervals 上：切掉相交部分，再把 slot 插回去。"""
    kept: list[TimelineSlot] = []
    for cur in intervals:
        if cur.end <= slot.start or cur.start >= slot.end:  # 不相交，原样保留
            kept.append(cur)
            continue
        if cur.start < slot.start:  # 左边剩下的
            kept.append(TimelineSlot(cur.start, slot.start, cur.level))
        if cur.end > slot.end:  # 右边剩下的
            kept.append(TimelineSlot(slot.end, cur.end, cur.level))
    kept.append(slot)
    kept.sort(key=lambda s: s.start)
    intervals[:] = kept


def _as_slots(timeline: Any) -> Sequence[TimelineSlot]:
    """允许直接传配置文本（解析失败照抛 TimelineError）。"""
    if timeline is None:
        return ()
    if isinstance(timeline, str):
        return parse_timeline(timeline)
    return timeline


def _hours_of(now: datetime) -> float:
    return now.hour + now.minute / 60.0 + now.second / 3600.0 + now.microsecond / 3.6e9


def availability_at(now: datetime, timeline: Any) -> str:
    """该时刻的可打扰度档位。

    timeline 可以是 ``parse_timeline`` 的结果，也可以直接是配置文本
    （文本解析失败会抛 TimelineError）。空时间线 → ``blocked``（安全默认）。
    """
    slots = _as_slots(timeline)
    if not slots:
        return BLOCKED
    hours = _hours_of(now)
    for slot in slots:
        if slot.contains(hours):
            return slot.level
    return BLOCKED


def next_speakable_start(now: datetime, timeline: Any) -> datetime | None:
    """下一个「非 blocked」时段的起点。

    当前就在可打扰时段里 → 返回 ``now``；整条时间线全是 blocked → ``None``。
    （next_window 的抖动以它为基准，测试也靠它验证抖动半径。）
    """
    slots = _as_slots(timeline)
    if not slots:
        return None
    if availability_at(now, slots) != BLOCKED:
        return now

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day_offset in (0, 1):
        base = midnight + timedelta(days=day_offset)
        for slot in slots:
            if slot.level == BLOCKED:
                continue
            start = base + timedelta(hours=slot.start)
            if start > now:
                return start
    return None


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ProactiveConfig:
    """``proactive`` 配置组。全部可配，默认与 _conf_schema.json 一致。

    ★ ``enable`` 默认 False：默认配置下主动开口**什么都不做**。
    """

    enable: bool = False
    session_list: frozenset[str] = frozenset()
    timeline: str = DEFAULT_TIMELINE
    probability: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PROBABILITY)
    )
    min_idle_minutes: float = 30.0
    """距上次**收到用户消息**至少空闲这么久（分钟）才考虑主动。0 = 不限制。"""
    cooldown_minutes: float = 45.0
    """同一会话两次主动之间的最小间隔（分钟）。"""
    daily_budget: int = 3
    """每个会话每天最多主动几条。0 = 一条都不发（保守）。"""
    max_unanswered: int = 2
    """连续主动未获回复达到此数就停手。0 = 不限制。"""
    jitter_minutes: float = 12.0
    """计划开口时刻的抖动半径（分钟）——真人不会准点。"""
    check_interval_seconds: float = 60.0
    """后台循环检查频率（秒），最小 5 秒，避免忙等。"""
    system_prompt: str = ""
    """可选：主动开口时额外的人设提示。留空 = 用 AstrBot 的人设。"""
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    """单次 text_chat 的硬超时（秒），最小 1 秒。★必须有，理由见模块 docstring。"""

    @property
    def active(self) -> bool:
        """是否真的可能产生副作用（= 会解析时间线 / 起循环 / 调 provider）。

        **enable=false 或 session_list 为空 → False**，调用方据此做到零副作用。
        """
        return self.enable and bool(self.session_list)

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> "ProactiveConfig":
        """从插件配置的 ``proactive`` 子 dict 构造。

        缺键用默认值、写坏也用默认值——配置填错不许把插件搞崩
        （唯一的例外是 ``timeline`` 文本：它要吵，由 main.py 捕获后退回默认时间线）。
        """
        data = coerce.as_mapping(raw)
        prob_raw = coerce.as_mapping(data.get("probability"))
        probability = {
            level: _clamp01(coerce.as_float(prob_raw.get(level), DEFAULT_PROBABILITY[level]))
            for level in LEVELS
        }
        return cls(
            enable=coerce.as_bool(data.get("enable"), False),
            session_list=coerce.as_id_list(data.get("session_list")),
            timeline=str(data.get("timeline") or DEFAULT_TIMELINE),
            probability=probability,
            min_idle_minutes=max(0.0, coerce.as_float(data.get("min_idle_minutes"), 30.0)),
            cooldown_minutes=max(0.0, coerce.as_float(data.get("cooldown_minutes"), 45.0)),
            daily_budget=max(0, coerce.as_int(data.get("daily_budget"), 3)),
            max_unanswered=max(0, coerce.as_int(data.get("max_unanswered"), 2)),
            jitter_minutes=max(0.0, coerce.as_float(data.get("jitter_minutes"), 12.0)),
            check_interval_seconds=max(
                5.0, coerce.as_float(data.get("check_interval_seconds"), 60.0)
            ),
            system_prompt=str(data.get("system_prompt") or ""),
            prompt_template=str(data.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE),
            llm_timeout_seconds=max(
                1.0, coerce.as_float(data.get("llm_timeout_seconds"), DEFAULT_LLM_TIMEOUT_SECONDS)
            ),
        )


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# ----------------------------------------------------------------------
# 每会话状态
# ----------------------------------------------------------------------
@dataclass
class DailyState:
    """一个会话的主动开口状态（按 umo 各一份，重启即清空）。

    时间统一用 **epoch 秒**（``datetime.timestamp()``），跨天判定用 ``date``。
    """

    umo: str = ""
    day: date | None = None
    """当前统计归属的日期。``None`` = 还没记过任何东西。"""
    sent: int = 0
    """今日已主动几条。"""
    last_sent_at: float = 0.0
    """上次主动开口的时间（epoch 秒）。"""
    last_user_at: float = 0.0
    """上次**收到用户消息**的时间（epoch 秒）。0 = 还没有过。"""
    unanswered: int = 0
    """连续主动未获回复的条数（收到用户消息就清零）。"""
    cooldown_until: float = 0.0
    """冷却到什么时候（epoch 秒）。"""

    def roll_day(self, today: date) -> bool:
        """跨天自动重置「今日已发条数」。返回是否发生了重置。

        刻意**只重置今日计数**：``unanswered`` 不因为过了零点就当没发生
        （昨天没理我，今天零点也不该立刻又开始搭话），
        ``cooldown_until`` / ``last_sent_at`` 是绝对时间，本来就会自然过期。
        """
        if self.day == today:
            return False
        first_time = self.day is None
        self.day = today
        if not first_time:
            self.sent = 0
            return True
        return False

    def note_sent(self, now: datetime, cooldown_minutes: float) -> None:
        """记一条「真的发出去了」。预算、冷即、未回复计数都在这里累加。"""
        self.roll_day(now.date())
        stamp = now.timestamp()
        self.sent += 1
        self.last_sent_at = stamp
        self.unanswered += 1
        self.cooldown_until = stamp + max(0.0, float(cooldown_minutes)) * 60.0

    def note_user_message(self, now: datetime) -> None:
        """收到用户消息：空闲计时重新开始，未回复计数清零。

        **不动冷却**：冷却约束的是「两次主动之间的最小间隔」，
        对方回了一句话不代表可以马上又主动开口。
        """
        self.last_user_at = now.timestamp()
        self.unanswered = 0


# ----------------------------------------------------------------------
# 决策
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ProactiveDecision:
    allow: bool
    reason: str
    level: str
    probability: float
    activity: float
    detail: str = ""

    def log_line(self) -> str:
        head = (
            f"[{'发送' if self.allow else '跳过'}] {self.reason}"
            f" 档位={self.level or '-'} P={self.probability:.3f} 活跃度={self.activity:.2f}"
        )
        return f"{head} | {self.detail}" if self.detail else head


def _reject(
    reason: str,
    level: str,
    probability: float,
    activity: float,
    detail: str,
) -> ProactiveDecision:
    return ProactiveDecision(False, reason, level, probability, activity, detail)


def should_speak(
    now: datetime,
    state: DailyState,
    config: ProactiveConfig,
    rng: Any,
    *,
    timeline: Any = None,
    activity: float = 1.0,
    sleeping: bool = False,
) -> ProactiveDecision:
    """主动开口的完整判定，返回「发/不发 + 原因 + 档位 + 概率」。

    注入项：
    - ``now``：当前时间（main 传 ``datetime.now()``）
    - ``state``：该会话的 DailyState（**会被就地更新**：跨天重置）
    - ``rng``：需要有 ``random()``（决策）与 ``uniform(a, b)``（next_window）的对象
    - ``timeline``：``parse_timeline`` 的结果；None 时用 ``config.timeline`` 文本
    - ``activity``：来自 ``schedule.activity_ratio``（0~1），**乘进概率**
    - ``sleeping``：来自 ``schedule.sleeping``（真 → 绝不主动）

    ★ ``enable=false`` 时**在解析时间线之前**就返回（零副作用）。
    """
    activity = _clamp01(activity)

    # 0) 总开关 / 白名单：最便宜、最硬的先判，绝不先解析时间线
    if not config.enable:
        return _reject(REASON_DISABLED, "", 0.0, activity, "proactive.enable = false")
    if not config.session_list:
        return _reject(
            REASON_EMPTY_LIST, "", 0.0, activity, "proactive.session_list 为空（保守：不生效）"
        )
    if not state.umo or state.umo not in config.session_list:
        return _reject(
            REASON_NOT_IN_LIST, "", 0.0, activity, f"{state.umo or '(未命名会话)'} 不在 session_list"
        )

    # 1) 作息表主权：睡着了绝不主动（复用我们自己的 schedule，不听别人指挥）
    if sleeping:
        return _reject(REASON_SLEEPING, "", 0.0, activity, "作息表判定为睡眠时段")

    slots = _as_slots(timeline if timeline is not None else config.timeline)
    level = availability_at(now, slots)

    # 2) 概率 = 档位概率 × 活跃度（活跃度 0 时自然为 0）
    probability = _clamp01(config.probability.get(level, 0.0)) * activity

    # 3) 跨天先重置今日计数，后面几道闸门才有意义
    state.roll_day(now.date())
    stamp = now.timestamp()

    # 4) blocked 档 + 概率 0 = 硬闸门（见模块 docstring 的说明）
    if probability <= 0.0:
        if level == BLOCKED:
            return _reject(
                REASON_BLOCKED,
                level,
                probability,
                activity,
                "可打扰度=blocked（请勿打扰档）",
            )
        return _reject(
            REASON_PROBABILITY,
            level,
            probability,
            activity,
            f"档位概率 × 活跃度 = 0（档位={level}，活跃度={activity:.2f}）",
        )

    # 5) 未回复超限：连着主动几条都没人理，就闭嘴
    if config.max_unanswered > 0 and state.unanswered >= config.max_unanswered:
        return _reject(
            REASON_UNANSWERED,
            level,
            probability,
            activity,
            f"连续 {state.unanswered} 条主动未获回复（上限 {config.max_unanswered}）",
        )

    # 6) 每日预算
    if state.sent >= config.daily_budget:
        return _reject(
            REASON_BUDGET,
            level,
            probability,
            activity,
            f"今日已发 {state.sent} 条（预算 {config.daily_budget}）",
        )

    # 7) 同一会话冷却
    if stamp < state.cooldown_until:
        left = state.cooldown_until - stamp
        return _reject(
            REASON_COOLDOWN,
            level,
            probability,
            activity,
            f"距上次主动还有 {left / 60:.1f} 分钟冷却（共 {config.cooldown_minutes:g} 分钟）",
        )

    # 8) 空闲不足：距上次收到用户消息太近就别插话（last_user_at=0 = 还不知道，跳过）
    if config.min_idle_minutes > 0 and state.last_user_at > 0:
        idle = stamp - state.last_user_at
        if idle < config.min_idle_minutes * 60.0:
            return _reject(
                REASON_IDLE,
                level,
                probability,
                activity,
                f"距上次收到消息只过了 {idle / 60:.1f} 分钟"
                f"（要求 ≥ {config.min_idle_minutes:g} 分钟）",
            )

    # 9) 掷骰子
    if rng.random() >= probability:
        return _reject(
            REASON_PROBABILITY,
            level,
            probability,
            activity,
            f"掷骰子没过（P={probability:.3f}）",
        )

    return ProactiveDecision(True, REASON_ALLOW, level, probability, activity, "")


def next_window(
    now: datetime,
    config: ProactiveConfig,
    rng: Any,
    *,
    timeline: Any = None,
) -> datetime | None:
    """下一个候选开口时刻（**含抖动**），返回 datetime；整条时间线全 blocked → None。

    基准是 ``next_speakable_start``：当前在可打扰时段 → 基准 = now；
    当前 blocked → 基准 = 下一个非 blocked 时段的起点。
    在基准上叠加 ``uniform(-jitter, +jitter)`` 分钟的抖动，然后保证：

    - 结果 **不早于 now**（不会把计划排到过去）
    - 结果 **不早于基准**：基准就是窗口起点，反向抖动必然落进上一个（blocked）档，
      会被夹回基准。所以实际效果是「**只往后抖**，宁可准点也不提前」——
      提前到「请勿打扰」的时段比准点更糟
    - 结果**不落进 blocked 档**（窗口开着时也一样：抖动过头就退回基准）

    只用注入的 rng 抽一次随机数，所以同一个种子必然得到同一个结果。
    main.py 用它给「冷却结束时刻」加抖动（真人不会准点）。
    """
    slots = _as_slots(timeline if timeline is not None else config.timeline)
    base = next_speakable_start(now, slots)
    if base is None:
        return None

    radius = max(0.0, float(config.jitter_minutes)) * 60.0
    offset = rng.uniform(-radius, radius) if radius > 0 else 0.0
    if base <= now:
        # 窗口已经开着：抖动只能往后拖（不能把计划排到过去）
        candidate = now + timedelta(seconds=abs(offset))
    else:
        candidate = base + timedelta(seconds=offset)

    if candidate < now:
        candidate = now
    if availability_at(candidate, slots) == BLOCKED:
        candidate = base if base > now else now
    return candidate


# ----------------------------------------------------------------------
# 内容处理（纯函数，供 main.py 调用）
# ----------------------------------------------------------------------
def split_message(
    text: str,
    *,
    max_chars: int = SEGMENT_MAX_CHARS,
    max_parts: int = SEGMENT_MAX_PARTS,
) -> list[str]:
    """长内容拆成几段依次发（真人不会把一大段一口气糊上去）。

    按句号/问号/感叹号/省略号/换行切句，再贪心装箱到每段 ≤ ``max_chars``；
    单句本身超长就硬切。段数超过 ``max_parts`` 时，前 ``max_parts-1`` 段照发、
    剩下的并进最后一段（**宁可最后一段长一点，也不静默丢内容**）。

    空/纯空白 → ``[]``（main.py 据此丢弃）。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if max_chars <= 0 or len(cleaned) <= max_chars:
        return [cleaned]

    parts: list[str] = []
    buffer = ""
    for sentence in (s.strip() for s in _SENTENCE_RE.findall(cleaned)):
        if not sentence:
            continue
        while len(sentence) > max_chars:  # 单句超长：硬切
            head, sentence = sentence[:max_chars], sentence[max_chars:]
            if buffer:
                parts.append(buffer)
                buffer = ""
            parts.append(head)
        if buffer and len(buffer) + len(sentence) > max_chars:
            parts.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        parts.append(buffer)

    if len(parts) <= max(1, max_parts):
        return parts
    keep = max(1, max_parts) - 1
    return parts[:keep] + ["".join(parts[keep:])]


def extract_text(response: Any) -> str:
    """从 ``provider.text_chat`` 的返回值里取正文。

    AstrBot 4.x 返回 ``LLMResponse``（有 ``completion_text``），但不同版本/适配器
    可能给 str 或 dict。取不到就返回空串——调用方据此跳过，绝不把对象 repr 发出去。
    """
    if response is None:
        return ""
    if isinstance(response, str):
        return response.strip()
    text = getattr(response, "completion_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    if isinstance(response, Mapping):
        for key in ("completion_text", "text", "content"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def compose_prompt(config: ProactiveConfig) -> tuple[str, str]:
    """返回 ``(prompt, system_prompt)``。system_prompt 为空 = 用 AstrBot 的人设。"""
    prompt = (config.prompt_template or "").strip() or DEFAULT_PROMPT_TEMPLATE
    system = (config.system_prompt or "").strip()
    return prompt, system
