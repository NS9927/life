"""低活跃度时的「攒一波，统一回」调度。

按活跃度分三档处理**被点名**（被 @ / 私聊）的消息：

    活跃度 ≥ immediate_threshold      立刻回（闲着的时候秒回很正常）
    0 < 活跃度 < immediate_threshold   攒 batch_window_seconds，然后统一回一次
    活跃度 = 0（睡眠时段）            一条都不回，排队到起床补发（见 queue.py）

为什么是「延迟第一条、后面的并进来」，而不是「每条都回一遍」：

1. **省 token**：N 条消息只产生 1 次 LLM 调用，剩下的在流水线里就被 stop 了。
2. **更像人**：上课/上班的人不会秒回；下课了会把攒的消息一起看完，回一句。
3. **上下文不丢**：群消息已经被内置 ``persist_group_message`` 写进历史了
   （它的 priority 是 ``maxsize - 2``，比本插件高，先于我们执行），
   所以延迟后的那次 LLM 调用本来就看得见群里聊了什么；
   私聊没有那套历史注入，所以我们把攒下的正文直接拼进 ``message_str``。

纯逻辑：时间从外面传进来，不 import asyncio、不 import astrbot，所以能单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field

ACTION_IMMEDIATE = "immediate"
ACTION_LEAD = "lead"
ACTION_FOLD = "fold"


@dataclass(frozen=True)
class BatchDecision:
    """一次「被点名」消息该怎么处理。"""

    action: str
    wait_seconds: float = 0.0
    buffered: int = 0
    reason: str = ""

    @property
    def is_fold(self) -> bool:
        return self.action == ACTION_FOLD

    @property
    def is_lead(self) -> bool:
        return self.action == ACTION_LEAD


@dataclass
class _Batch:
    opened_at: float
    texts: list[str] = field(default_factory=list)


class BatchPlanner:
    """按会话维护「待统一回复」的批次。有状态，但状态只有时间和文本。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        window_seconds: float = 120.0,
        immediate_threshold: float = 0.6,
        max_messages: int = 10,
    ):
        self.enabled = enabled
        self.window_seconds = max(0.0, float(window_seconds))
        self.immediate_threshold = min(1.0, max(0.0, float(immediate_threshold)))
        self.max_messages = max(1, int(max_messages))
        self._batches: dict[str, _Batch] = {}

    # ------------------------------------------------------------------
    def decide(self, umo: str, text: str, activity: float, now: float) -> BatchDecision:
        """决定这条被点名的消息是「立刻回」「当领队等窗口」还是「并进批次」。"""
        if not self.enabled or activity >= self.immediate_threshold:
            return BatchDecision(ACTION_IMMEDIATE, reason="活跃度够高，直接回")

        batch = self._batches.get(umo)
        if batch is not None and now - batch.opened_at <= self.window_seconds:
            batch.texts.append(text)
            return BatchDecision(
                ACTION_FOLD,
                buffered=len(batch.texts),
                reason=f"活跃度低，并入待回批次（第 {len(batch.texts)} 条）",
            )

        # 没有批次 / 批次已过期 → 这条当领队，等满一个窗口再统一回
        self._batches[umo] = _Batch(opened_at=now, texts=[text])
        return BatchDecision(
            ACTION_LEAD,
            wait_seconds=self.window_seconds,
            buffered=1,
            reason=f"活跃度低，先攒 {self.window_seconds:.0f} 秒再统一回",
        )

    # ------------------------------------------------------------------
    def finish(self, umo: str) -> str:
        """领队等完窗口后取走合并文案，并清掉批次。"""
        batch = self._batches.pop(umo, None)
        if batch is None:
            return ""
        return compose_batch_text(batch.texts, max_messages=self.max_messages)

    def active(self, umo: str) -> bool:
        return umo in self._batches

    def buffered(self, umo: str) -> int:
        batch = self._batches.get(umo)
        return len(batch.texts) if batch else 0

    def pending(self) -> dict[str, int]:
        return {umo: len(batch.texts) for umo, batch in self._batches.items()}

    def drop(self, umo: str) -> None:
        self._batches.pop(umo, None)


def compose_batch_text(
    texts: list[str],
    *,
    max_messages: int = 10,
    max_chars: int = 2000,
) -> str:
    """把一批消息合成一条给 LLM 看的正文。

    超过 max_messages 条时只保留**最新**的若干条，并在开头标明还有多少条没看——
    旧的已经过时了，堆上去只会稀释重点。
    """
    cleaned = [" ".join(t.split()) for t in texts if t and t.strip()]
    if not cleaned:
        return ""

    dropped = 0
    if len(cleaned) > max_messages:
        dropped = len(cleaned) - max_messages
        cleaned = cleaned[-max_messages:]

    body = "\n".join(cleaned)
    if dropped:
        body = f"（前面还有 {dropped} 条没看）\n{body}"
    if len(body) > max_chars:
        body = body[:max_chars] + "…"
    return body
