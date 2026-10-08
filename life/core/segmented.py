"""分段回复的时间模型：**先读、再打、段间抖动**（模仿人在打字）。

## 为什么不用内置的 ``platform_settings.segmented_reply``

AstrBot 内置确实能分段、也能给段间加**随机**间隔（已核对 4.28.2 源码）：

- ``core/pipeline/result_decorate/stage.py:208-267``：按 ``split_mode``（regex / words）
  把 Plain 文本切成多个消息段，``words_count_threshold`` 是「超过就不切」的上限。
- ``core/pipeline/respond/stage.py:98-107``：``_calc_comp_interval`` 算段间间隔——
  ``interval_method=random`` → ``random.uniform(interval[0], interval[1])``；
  ``interval_method=log`` → ``random.uniform(log(字数+1, log_base), 该值+0.5)``。
- ``core/pipeline/respond/stage.py:262-290``：逐段 ``asyncio.sleep(i)`` 后
  ``event.send(result.derive([comp]))``。

**但内置做不到我们要的时间模型**：

1. 只有「回复段字数 → 间隔」这一个变量（log 法），**没有阅读入站消息**这一段；
2. 速度旋钮是 ``log_base``（对数底数），不是「字/秒」，没法按用户直觉调；
3. 抖动是固定绝对量（log 法 ±0.5s；random 法是固定区间），**没有比例抖动**；
4. **没有总耗时上限**：段多时线性叠加，可能把事件挂很久；
5. **没有段数上限 / 最短段合并**（只有空段丢弃）；
6. 对**每一段都先 sleep**（``stage.py:276-278`` 在 send 之前），包括第一段——
   等于回复首字固定再晚 2 秒左右；
7. 是**全局 AstrBot 配置**，对所有会话生效，插件也无法注入 rng、无法参与决策。

所以本模块自己实现时间模型；main.py 在 ``on_decorating_result`` 里接管前 N-1 段，
最后一段仍交给流水线（保证不重不漏）。

## 时间模型

    # 第 1 段之前（先读你说的，再组织并打字）
    reading   = 入站消息字数 / reading_chars_per_second
    typing    = 本条回复总字数 / typing_chars_per_second
    pre_delay = clamp((reading + typing) × jitter_factor, pre_min_seconds, pre_max_seconds)

    # 第 i 段与第 i+1 段之间（还在打字）
    seg_delay = clamp(第 i 段字数 / typing_chars_per_second × jitter_factor,
                      min_seconds, max_seconds)

    jitter_factor = uniform(1 - jitter, 1 + jitter)      # 每次抽样独立

    # 总上限：pre 与所有段间隔之和超过 max_total_seconds 就等比压缩，
    # 但每项保底 × FLOOR_RATIO（见下面注释），绝不压成 0 变成连发。

⚠️ 关于 clamp 的位置：规格写的是「先 clamp 再乘抖动」。本模块**先乘抖动再 clamp**，
这样 ``pre_min/pre_max``、``min/max_seconds`` 是**硬边界**（测试和用户预期都按这个来），
同时在区间中间抖动照常生效；如果按「先 clamp 后抖动」，上限会被抖动突破 1+jitter 倍。
两者都带抖动，这里选「配置的上下限说话算数」这一种，并在报告里注明。

本模块是**纯逻辑**：不 import astrbot、不 sleep、不自己取时间，rng 由外面注入，
所以同一串输入必然得到同一串结果，可以直接单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import coerce

DEFAULT_SPLIT_CHARS = "。！？!?\n…；;"
"""在这些字符**之后**切分（标点保留在前一段末尾）。"""

FLOOR_RATIO = 0.3
"""``max_total_seconds`` 压缩后每项的保底比例：

    保底 = 该项的配置下限 × 0.3（pre 用 pre_min_seconds，段间用 min_seconds）

压缩是「等比缩小」，如果缩到比保底还小，就以保底为准——**允许总耗时略微超上限**，
也不允许变成 0 秒连发（连发比慢一点更像机器人）。
"""


@dataclass(frozen=True)
class SegmentedJitterConfig:
    """``segmented_jitter`` 配置组。★默认关闭：默认配置下什么都不做。"""

    enable: bool = False
    typing_chars_per_second: float = 3.5
    """中文打字速度（字/秒）。人约 2~5，调大 = 打字更快、段间更短。"""
    reading_chars_per_second: float = 12.0
    """阅读速度（字/秒）。人约 8~15，只影响第 1 段之前的等待。"""
    pre_min_seconds: float = 0.8
    """第 1 段之前的等待下限。"""
    pre_max_seconds: float = 15.0
    """第 1 段之前的等待上限（别让人等太久）。

    ★「长回复打字更久」就是靠这个上限撑开的：打字时间本身是
    ``回复字数 / typing_chars_per_second``（线性、无上限），被它截住。
    默认 15s + 默认 3.5 字/秒 ⇒ 约 52 字以内越长越久，之后**到顶不再增长**
    （再长就变成让人干等，不像打字像卡住）；嫌短/嫌长直接调这个值。"""
    min_seconds: float = 0.6
    """段间间隔下限。"""
    max_seconds: float = 3.0
    """段间间隔上限。"""
    jitter: float = 0.25
    """抖动比例：实际 = base × uniform(1-jitter, 1+jitter)。0 = 不抖。"""
    max_segments: int = 4
    """最多切几段，超出的并进最后一段。"""
    max_total_seconds: float = 30.0
    """整条回复的总耗时上限（含第 1 段之前），超了等比压缩。0 = 不限制。

    它是第二道闸：``pre_max_seconds`` 管单次等待，这个管整条回复的总时长
    （长回复 + 多段时防止把事件挂到一分钟以上）。"""
    split_chars: str = DEFAULT_SPLIT_CHARS
    """在这些字符之后切分（保留标点）。"""
    min_segment_chars: int = 6
    """太短的段不单独发，并进相邻段。"""
    count_incoming_chars: bool = True
    """是否把入站消息字数算进「阅读」时间。关掉 = 只按回复长度算。"""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> "SegmentedJitterConfig":
        """从插件配置的 ``segmented_jitter`` 子 dict 构造。

        缺键用默认值、写坏也用默认值——配置填错不许把回复搞崩。
        会把 pre_min/pre_max、min/max 归一化成合法区间（写反了就交换）。
        """
        data = coerce.as_mapping(raw)

        pre_min = max(0.0, coerce.as_float(data.get("pre_min_seconds"), 0.8))
        pre_max = max(0.0, coerce.as_float(data.get("pre_max_seconds"), 15.0))
        if pre_min > pre_max:
            pre_min, pre_max = pre_max, pre_min

        seg_min = max(0.0, coerce.as_float(data.get("min_seconds"), 0.6))
        seg_max = max(0.0, coerce.as_float(data.get("max_seconds"), 3.0))
        if seg_min > seg_max:
            seg_min, seg_max = seg_max, seg_min

        split_chars = data.get("split_chars")
        if not isinstance(split_chars, str) or not split_chars:
            split_chars = DEFAULT_SPLIT_CHARS

        return cls(
            enable=coerce.as_bool(data.get("enable"), False),
            # 速度给个正的下限，避免除零；写 0 视为「快得没有延迟」
            typing_chars_per_second=max(
                0.1, coerce.as_float(data.get("typing_chars_per_second"), 3.5)
            ),
            reading_chars_per_second=max(
                0.1, coerce.as_float(data.get("reading_chars_per_second"), 12.0)
            ),
            pre_min_seconds=pre_min,
            pre_max_seconds=pre_max,
            min_seconds=seg_min,
            max_seconds=seg_max,
            jitter=max(0.0, min(1.0, coerce.as_float(data.get("jitter"), 0.25))),
            max_segments=max(1, coerce.as_int(data.get("max_segments"), 4)),
            max_total_seconds=max(0.0, coerce.as_float(data.get("max_total_seconds"), 30.0)),
            split_chars=split_chars,
            min_segment_chars=max(0, coerce.as_int(data.get("min_segment_chars"), 6)),
            count_incoming_chars=coerce.as_bool(data.get("count_incoming_chars"), True),
        )


# ----------------------------------------------------------------------
# 切分
# ----------------------------------------------------------------------
def split_text(text: str, config: SegmentedJitterConfig) -> list[str]:
    """按 ``split_chars`` 把回复切成多段（标点保留在前一段末尾）。

    规则（顺序固定）：

    1. 整段先 ``strip()``（首尾空白本来也看不见）；空/纯空白 → ``[]``
    2. 在 ``split_chars`` 里的字符**之后**切开，字符本身留在前一段
    3. 太短的段（``< min_segment_chars``，按 strip 后长度算）并进**前**一段；
       第一段太短就并进后一段——不让「嗯。」单独占一条消息
    4. 段数超过 ``max_segments`` → 前 ``max_segments-1`` 段照发，
       剩下的**全部并进最后一段**（宁可最后一段长一点，也不静默丢内容）

    返回的每段都是原文的连续片段，``"".join(结果) == text.strip()``——
    不增删改任何字符（除了整体首尾空白）。
    没有可切分字符时返回单段（调用方据此「原样放行」）。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if not config.split_chars:
        return [cleaned]

    pieces: list[str] = []
    buffer = ""
    for char in cleaned:
        buffer += char
        if char in config.split_chars:
            pieces.append(buffer)
            buffer = ""
    if buffer:
        pieces.append(buffer)
    if not pieces:
        return [cleaned]

    pieces = _merge_short(pieces, config.min_segment_chars)
    return _cap_segments(pieces, config.max_segments)


def _merge_short(pieces: list[str], min_chars: int) -> list[str]:
    """把过短的段并进相邻段（当前段太短就并进前一段；首段太短就并进后一段）。"""
    if min_chars <= 0:
        return list(pieces)

    merged: list[str] = []
    for piece in pieces:
        if merged and len(piece.strip()) < min_chars:
            merged[-1] = merged[-1] + piece  # 当前段太短 → 并进前一段
        else:
            merged.append(piece)

    # 第一段过短：往后并（可能连续几次）
    while len(merged) > 1 and len(merged[0].strip()) < min_chars:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    return merged


def _cap_segments(pieces: list[str], max_segments: int) -> list[str]:
    """段数上限：超出的并进最后一段。"""
    limit = max(1, int(max_segments))
    if len(pieces) <= limit:
        return list(pieces)
    return pieces[: limit - 1] + ["".join(pieces[limit - 1 :])]


# ----------------------------------------------------------------------
# 时间模型
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class TimingPlan:
    """一次回复的等待计划。``segment_delays[i]`` = 第 i 段与第 i+1 段之间的间隔。"""

    incoming_chars: int
    reply_chars: int
    reading_seconds: float
    typing_seconds: float
    pre_delay: float
    segment_delays: tuple[float, ...] = ()
    total: float = 0.0
    scaled: bool = False
    """是否因为 ``max_total_seconds`` 做过等比压缩。"""
    floors_applied: bool = False
    """压缩后是否触到了保底（触到就可能略微超上限）。"""

    def log_line(self) -> str:
        segments = "/".join(f"{d:.2f}" for d in self.segment_delays) or "-"
        tail = f"（已按总上限压缩{f'，触保底' if self.floors_applied else ''}）" if self.scaled else ""
        return (
            f"入站 {self.incoming_chars} 字，阅读 {self.reading_seconds:.2f}s，"
            f"打字 {self.typing_seconds:.2f}s（回复 {self.reply_chars} 字），"
            f"pre_delay {self.pre_delay:.2f}s，段间隔 {segments}s，"
            f"总 {self.total:.2f}s{tail}"
        )


def jitter_factor(rng: Any, jitter: float) -> float:
    """抖动系数：``uniform(1-jitter, 1+jitter)``；jitter=0 → 1.0。"""
    ratio = max(0.0, min(1.0, float(jitter)))
    if ratio <= 0:
        return 1.0
    return float(rng.uniform(1.0 - ratio, 1.0 + ratio))


def _clamp(value: float, low: float, high: float) -> float:
    if high < low:
        low, high = high, low
    return max(low, min(high, value))


def plan_timing(
    incoming_chars: int,
    segments: Sequence[str],
    config: SegmentedJitterConfig,
    rng: Any,
) -> TimingPlan:
    """按字数算出「先读再打 + 段间抖动」的完整等待计划。

    - 注入的 rng 决定抖动（同一种子 → 同一结果），每次抽样独立
    - 每一段间隔都用**上一段**的字数算（打上一段花了多久）
    - ``max_total_seconds`` 超了就等比压缩，每项保底 ``下限 × FLOOR_RATIO``
    """
    parts = list(segments or [])
    reply_chars = sum(len(part) for part in parts)

    reading = (
        max(0, int(incoming_chars)) / config.reading_chars_per_second
        if config.count_incoming_chars
        else 0.0
    )
    typing = reply_chars / config.typing_chars_per_second

    pre_delay = _clamp(
        (reading + typing) * jitter_factor(rng, config.jitter),
        config.pre_min_seconds,
        config.pre_max_seconds,
    )

    segment_delays: list[float] = []
    for part in parts[:-1]:  # 段间隔只存在于相邻两段之间：N 段 → N-1 个
        base = len(part) / config.typing_chars_per_second
        segment_delays.append(
            _clamp(
                base * jitter_factor(rng, config.jitter),
                config.min_seconds,
                config.max_seconds,
            )
        )

    total = pre_delay + sum(segment_delays)
    scaled = False
    floors_applied = False
    cap = config.max_total_seconds
    if cap > 0 and total > cap:
        scale = cap / total
        floors = [config.pre_min_seconds * FLOOR_RATIO] + [
            config.min_seconds * FLOOR_RATIO
        ] * len(segment_delays)
        values = [pre_delay] + segment_delays
        scaled_values = [max(value * scale, floor) for value, floor in zip(values, floors)]
        floors_applied = any(
            value * scale < floor for value, floor in zip(values, floors)
        )
        pre_delay = scaled_values[0]
        segment_delays = scaled_values[1:]
        total = sum(scaled_values)
        scaled = True

    return TimingPlan(
        incoming_chars=max(0, int(incoming_chars)),
        reply_chars=reply_chars,
        reading_seconds=reading,
        typing_seconds=typing,
        pre_delay=pre_delay,
        segment_delays=tuple(segment_delays),
        total=total,
        scaled=scaled,
        floors_applied=floors_applied,
    )
