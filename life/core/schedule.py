"""作息表：时段权重 → 0~1 活跃度。

设计依据：docs/可行性报告与设计.md 第一节（作息表）与第二节（推荐公式）。

    活跃度 = clamp01( 插值后的时段权重 / 100 × 周末系数 )

活跃度是**概率乘数**，不是频率：0.3 表示「每条消息 30% 概率能发出去」。
权重 0 的时段 = 睡眠时段，独立处理（点名排队、群聊直接丢），见 gate.py。

本模块是**纯函数**：输入 datetime，输出数字，不碰 AstrBot、不碰 I/O、不碰随机数。
所以它最好测，也确实先写它。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# 与 _conf_schema.json 的 time_weights.default 保持一致
DEFAULT_TIME_WEIGHTS = "0-2=80\n2-8=0\n8-10=10\n10-12=30\n12-14=40\n14-19=30\n19-23=80\n23-24=60"

_LINE_RE = re.compile(
    r"^\s*(?P<start>\d{1,2}(?:\.\d+)?)\s*-\s*(?P<end>\d{1,2}(?:\.\d+)?)\s*=\s*(?P<weight>\d{1,3}(?:\.\d+)?)\s*$"
)

DAY_HOURS = 24.0


@dataclass(frozen=True)
class Segment:
    """一段连续的时段。start/end 是小时（0~24，允许 end == 24）。"""

    start: float
    end: float
    weight: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class TimeWeightsError(ValueError):
    """time_weights 配置文本解析失败。"""


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def parse_time_weights(text: str) -> list[Segment]:
    """把配置文本解析成有序、连续、覆盖 [0, 24] 的时段列表。

    每行格式：``起始小时-结束小时=权重``，例如 ``2-8=0``。
    - ``#`` 开头或空行忽略
    - ``end < start`` 视为跨零点，自动拆成 [start,24] + [0,end]
    - 解析完成后补齐首尾空隙，保证任意时刻都能查到权重
    - 格式错误直接抛 TimeWeightsError（配置错了要吵，不要静默用默认值）
    """
    if not text or not text.strip():
        raise TimeWeightsError("time_weights 为空")

    raw: list[Segment] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE_RE.match(stripped)
        if not match:
            raise TimeWeightsError(f"第 {lineno} 行格式错误：{stripped!r}（应为 起始-结束=权重）")

        start = float(match.group("start"))
        end = float(match.group("end"))
        weight = float(match.group("weight"))

        if not 0 <= start < DAY_HOURS:
            raise TimeWeightsError(f"第 {lineno} 行起始小时越界：{start}（应在 0~23）")
        if not 0 < end <= DAY_HOURS:
            raise TimeWeightsError(f"第 {lineno} 行结束小时越界：{end}（应在 1~24）")
        if not 0 <= weight <= 100:
            raise TimeWeightsError(f"第 {lineno} 行权重越界：{weight}（应在 0~100）")
        if start == end:
            raise TimeWeightsError(f"第 {lineno} 行时段长度为 0：{stripped!r}")

        if end < start:  # 跨零点
            raw.append(Segment(start, DAY_HOURS, weight))
            raw.append(Segment(0.0, end, weight))
        else:
            raw.append(Segment(start, end, weight))

    if not raw:
        raise TimeWeightsError("time_weights 里没有任何有效时段")

    return _fill_and_sort(raw)


def _fill_and_sort(segments: list[Segment]) -> list[Segment]:
    """排序 + 补齐空隙，确保 [0,24] 被完整覆盖。"""
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    out: list[Segment] = []

    # 开头空隙：用第一段的权重填
    if ordered[0].start > 0:
        out.append(Segment(0.0, ordered[0].start, ordered[0].weight))

    for seg in ordered:
        if seg.end <= seg.start:  # 前一段被挤压后可能退化成零长度，直接丢
            continue
        if out and seg.start < out[-1].end:
            # 与前一段重叠：截断被覆盖的部分（后面的段优先，配置里靠后写的更晚生效）
            if seg.end <= out[-1].end:
                continue
            seg = Segment(out[-1].end, seg.end, seg.weight)
        elif out and seg.start > out[-1].end:
            # 中间空隙：用前一段权重填
            out.append(Segment(out[-1].end, seg.start, out[-1].weight))
        if seg.end <= seg.start:
            continue
        out.append(seg)

    # 结尾空隙：延长最后一段
    if out and out[-1].end < DAY_HOURS:
        out[-1] = Segment(out[-1].start, DAY_HOURS, out[-1].weight)

    return out


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def _wrap_hours(hours: float) -> float:
    return hours % DAY_HOURS


def _circular_delta(hours: float, cut: float) -> float:
    """hours 相对 cut 的带符号环形距离，落在 (-12, 12]。"""
    delta = (hours - cut) % DAY_HOURS
    if delta > DAY_HOURS / 2:
        delta -= DAY_HOURS
    return delta


def segment_at(hours: float, segments: list[Segment]) -> Segment | None:
    """取该时刻所属时段（不含插值，睡眠判定用它）。"""
    h = _wrap_hours(hours)
    for seg in segments:
        if seg.start <= h < seg.end:
            return seg
    return None


def weight_at(
    hours: float,
    segments: list[Segment],
    interpolate_minutes: float = 45.0,
) -> float:
    """该时刻的时段权重（0~100），在切点附近做线性插值。

    插值语义：以切点为中心 ±interpolate_minutes 的窗口内，
    权重从「切点前那段的权重」平滑过渡到「切点后那段的权重」——真人不会 8:00 准点换挡。
    多个切点同时落入窗口时，取**最近的**那个切点。

    interpolate_minutes <= 0 时退化为硬切换。
    """
    h = _wrap_hours(hours)
    seg = segment_at(h, segments)
    base = seg.weight if seg else 0.0

    half = float(interpolate_minutes) / 60.0
    if half <= 0 or len(segments) < 2:
        return base

    # 收集所有切点：段尾 == 下段头，外加环形接缝 24/0
    cuts: list[tuple[float, float, float]] = []  # (cut, before_w, after_w)
    n = len(segments)
    for i in range(n):
        cur = segments[i]
        nxt = segments[(i + 1) % n]
        gap = (nxt.start - cur.end) % DAY_HOURS
        if gap < 1e-9:
            cuts.append((_wrap_hours(cur.end), cur.weight, nxt.weight))

    best: tuple[float, float] | None = None  # (distance, value)
    for cut, w_before, w_after in cuts:
        delta = _circular_delta(h, cut)
        distance = abs(delta)
        if distance > half:
            continue
        ratio = 0.5 + delta / (2 * half)  # delta=-half → 0，delta=+half → 1
        value = w_before + (w_after - w_before) * ratio
        if best is None or distance < best[0]:
            best = (distance, value)

    return best[1] if best else base


def is_sleep(hours: float, segments: list[Segment]) -> bool:
    """是否处于睡眠时段。

    判定用**未插值**的原始权重：权重 0 的时段才算睡。
    插值窗口内权重是 0~10 之间的过渡值，不该被当成睡着——那只是「刚要睡/刚醒」。
    """
    seg = segment_at(hours, segments)
    return seg is None or seg.weight <= 0


# ----------------------------------------------------------------------
# 周末
# ----------------------------------------------------------------------
def shift_sleep(segments: list[Segment], hours: float) -> list[Segment]:
    """把权重为 0 的睡眠窗口整体后移 hours 小时（周末晚睡）。

    只挪「零权重窗口」的两条边，两边相邻的时段自动伸缩，保持全天连续覆盖。

    位移量会被夹在**相邻时段允许的范围**内：后移不能吃掉下一段，前移不能把上一段压成负长度。
    默认表（0-2 / 2-8 / 8-10 / …）下可挪范围是 −2 ~ +2 小时。
    """
    if hours == 0:
        return list(segments)

    idx = next((i for i, s in enumerate(segments) if s.weight <= 0), None)
    if idx is None:
        return list(segments)

    out = list(segments)
    zero = out[idx]
    n = len(out)

    shift = float(hours)
    if n >= 2:
        prev = out[(idx - 1) % n]
        nxt = out[(idx + 1) % n]
        if shift > 0:
            shift = min(shift, max(0.0, min(DAY_HOURS - zero.end, nxt.end - zero.end)))
        else:
            shift = max(shift, min(0.0, max(-zero.start, prev.start - zero.start)))
    else:
        shift = max(-zero.start, min(shift, DAY_HOURS - zero.end))

    if shift == 0:
        return out

    new_start = zero.start + shift
    new_end = zero.end + shift
    out[idx] = Segment(new_start, new_end, zero.weight)
    if n >= 2:
        prev = out[(idx - 1) % n]
        nxt = out[(idx + 1) % n]
        out[(idx - 1) % n] = Segment(prev.start, new_start, prev.weight)
        out[(idx + 1) % n] = Segment(new_end, nxt.end, nxt.weight)

    return _fill_and_sort(out)


def is_weekend(dt: datetime) -> bool:
    return dt.weekday() >= 5


def activity_ratio(
    dt: datetime,
    segments: list[Segment],
    *,
    interpolate_minutes: float = 45.0,
    weekend_multiplier: float = 1.3,
    weekend_sleep_shift_hours: float = 1.0,
) -> float:
    """该时刻的活跃度，0~1。这就是要乘进基础概率的那个数。

    周末（周六/周日）先整体后移睡眠窗口，再乘系数，最后夹到 [0, 1]。
    """
    segs = segments
    weekend = is_weekend(dt)
    if weekend and weekend_sleep_shift_hours:
        segs = shift_sleep(segs, weekend_sleep_shift_hours)

    hours = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    ratio = weight_at(hours, segs, interpolate_minutes) / 100.0
    if weekend:
        ratio *= weekend_multiplier
    return min(1.0, max(0.0, ratio))


def sleeping(
    dt: datetime,
    segments: list[Segment],
    *,
    weekend_sleep_shift_hours: float = 1.0,
) -> bool:
    """该时刻是否在睡眠时段（周末按后移后的窗口判断）。"""
    segs = segments
    if is_weekend(dt) and weekend_sleep_shift_hours:
        segs = shift_sleep(segs, weekend_sleep_shift_hours)
    hours = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    return is_sleep(hours, segs)


def describe(
    dt: datetime,
    segments: list[Segment],
    *,
    interpolate_minutes: float = 45.0,
    weekend_multiplier: float = 1.3,
    weekend_sleep_shift_hours: float = 1.0,
) -> str:
    """一行日志用的人类可读描述，例如 ``15:57 活跃度 0.30（工作日）``。"""
    ratio = activity_ratio(
        dt,
        segments,
        interpolate_minutes=interpolate_minutes,
        weekend_multiplier=weekend_multiplier,
        weekend_sleep_shift_hours=weekend_sleep_shift_hours,
    )
    asleep = sleeping(dt, segments, weekend_sleep_shift_hours=weekend_sleep_shift_hours)
    tag = "睡眠" if asleep else ("周末" if is_weekend(dt) else "工作日")
    return f"{dt:%H:%M} 活跃度 {ratio:.2f}（{tag}）"
