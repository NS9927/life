"""睡眠期消息队列：点名消息入队，起床后按序补发。

设计依据：docs/可行性报告与设计.md 第二节「睡眠时段单独处理」。

    被 @ / 私聊  → 排队，起床后按序补发
    群聊闲聊     → 直接丢（真人也不会翻回去补）

两个刻意的设计选择：

1. **每个会话只留最新的 N 条**。对方半夜连发十条，早上把这十条全部重放等于刷屏，
   也不是真人行为。留最新的，丢掉最旧的。
2. **全局按时间戳出队**，不按会话轮流——起床后先回最早的那条，才像「刚看到」。

纯数据结构，不依赖 AstrBot、不做 I/O；定时判定也只认传入的 datetime。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .gate import MessageKind


@dataclass(frozen=True)
class QueuedMessage:
    """一条等着被补发的消息。只存补发需要的东西。"""

    umo: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: float
    kind: MessageKind = MessageKind.ADDRESSED

    def describe(self) -> str:
        when = datetime.fromtimestamp(self.timestamp)
        who = self.sender_name or self.sender_id
        text = self.text if len(self.text) <= 40 else self.text[:40] + "…"
        return f"{when:%H:%M} {who}: {text}"


@dataclass(frozen=True)
class PushResult:
    accepted: bool
    evicted: QueuedMessage | None = None  # 因容量上限被挤掉的旧消息
    reason: str = ""


class SleepQueue:
    """按会话分组的睡眠期队列。"""

    def __init__(self, max_per_session: int = 5, max_age_hours: float = 12.0):
        self.max_per_session = max(1, int(max_per_session))
        self.max_age_hours = float(max_age_hours)
        self._by_session: "OrderedDict[str, list[QueuedMessage]]" = OrderedDict()

    # ------------------------------------------------------------------
    def push(self, message: QueuedMessage) -> PushResult:
        """入队。超容量时挤掉该会话最旧的一条（保留最新的）。"""
        if not message.text and not message.sender_id:
            return PushResult(False, reason="空消息")

        bucket = self._by_session.setdefault(message.umo, [])
        bucket.append(message)

        evicted = None
        if len(bucket) > self.max_per_session:
            evicted = bucket.pop(0)
        return PushResult(True, evicted=evicted, reason="" if evicted is None else "超出每会话上限")

    # ------------------------------------------------------------------
    def prune(self, now: float) -> list[QueuedMessage]:
        """丢掉太旧的消息（比如半夜三点入队、第二天中午才补发）。返回被丢掉的。"""
        cutoff = now - self.max_age_hours * 3600
        dropped: list[QueuedMessage] = []
        for umo in list(self._by_session):
            bucket = self._by_session[umo]
            keep = [m for m in bucket if m.timestamp >= cutoff]
            dropped.extend(m for m in bucket if m.timestamp < cutoff)
            if keep:
                self._by_session[umo] = keep
            else:
                del self._by_session[umo]
        return dropped

    # ------------------------------------------------------------------
    def drain(self, now: float | None = None) -> list[QueuedMessage]:
        """全部取出并清空，按时间戳升序（先回最早的）。"""
        if now is not None:
            self.prune(now)
        everything = [m for bucket in self._by_session.values() for m in bucket]
        self._by_session.clear()
        return sorted(everything, key=lambda m: m.timestamp)

    def drain_session(self, umo: str) -> list[QueuedMessage]:
        bucket = self._by_session.pop(umo, [])
        return sorted(bucket, key=lambda m: m.timestamp)

    def peek(self, umo: str | None = None) -> list[QueuedMessage]:
        if umo is not None:
            return list(self._by_session.get(umo, []))
        return sorted(
            (m for bucket in self._by_session.values() for m in bucket),
            key=lambda m: m.timestamp,
        )

    def counts(self) -> dict[str, int]:
        return {umo: len(bucket) for umo, bucket in self._by_session.items()}

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._by_session.values())

    def __bool__(self) -> bool:
        return len(self) > 0


# ----------------------------------------------------------------------
# 定时
# ----------------------------------------------------------------------
def next_flush_time(now: datetime, flush_at_hour: int) -> datetime:
    """下一次补发时刻（本地时间）。已过今天的点就说明是明天。"""
    candidate = now.replace(hour=int(flush_at_hour) % 24, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def due_for_flush(now: datetime, flush_at_hour: int, last_flush_date: date | None) -> bool:
    """今天该不该补发。

    条件：已过补发整点，且今天还没补发过。
    这样机器人半夜断了、早上十点才起来，一样会把队列补掉（不会因为错过 8:00 就永远不补）。
    """
    if now.hour < int(flush_at_hour) % 24:
        return False
    return last_flush_date != now.date()


def compose_flush_text(
    messages: list[QueuedMessage],
    *,
    max_quote: int = 3,
    opener: str = "（刚看到…昨晚睡着啦）",
) -> str:
    """把一晚积压的消息拼成一条补发文案。

    MVP 版：不做 LLM 回炉，只把「刚看到 + 谁说了什么」如实发出去。
    真人起床翻手机也就是这个反应；真要生成回复，得另接 provider，见 README 待办。
    """
    if not messages:
        return ""

    ordered = sorted(messages, key=lambda m: m.timestamp)
    shown = ordered[:max_quote]
    lines = [opener]
    for msg in shown:
        who = msg.sender_name or msg.sender_id
        text = " ".join(msg.text.split())
        lines.append(f"{who}：{text}" if text else who)
    hidden = len(ordered) - len(shown)
    if hidden > 0:
        lines.append(f"（还有 {hidden} 条没看完…）")
    return "\n".join(lines)
