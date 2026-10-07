"""回复延迟：替代固定秒回。

设计依据：docs/可行性报告与设计.md 第二节「推荐公式」与第四节 delay.py。

真人不是 0.5 秒秒回的：越不活跃的时候（上课、上班、刚醒），回得越慢。
所以延迟 = 基础随机延迟 + (1 − 活跃度) × 浮动幅度 [+ 睡眠额外]。

注意：本模块只算「等多久」，不碰 @ 和引用——那是 astrbot_plugin_at_inline 的职责。
纯函数：随机数从外面注入，同样的种子给同样的结果。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import coerce


@dataclass(frozen=True)
class DelayConfig:
    """与 _conf_schema.json 的 reply_delay 对应。"""

    enable: bool = True
    min_seconds: float = 0.5
    max_seconds: float = 1.5
    sleep_extra_seconds: float = 20.0
    activity_penalty: float = 0.0
    """活跃度低时的额外延迟系数：额外 = (1−活跃度) × (max−min) × 本系数。

    调参历史（都是真机反馈驱动）：
    - 最初 30 秒 + 系数 1.0 → 活跃度 0.3 时要等 21~50 秒，群里抱怨「延迟又起来了」
    - 改成 8 秒 + 0.35 → 仍偏慢；再收到 1~4 秒
    - 现在 **0.5~1.5 秒、系数 0** —— 实测端到端 = LLM 0.7~4.6 秒 + 本延迟 + 发送 1~2 秒，
      那 1~4 秒叠上去总量就到 3~10 秒了，所以只留一点点「不是秒回」的手感
      （低活跃度「回得慢」交给攒批窗口表达，不叠在单条延迟上）
    """

    @classmethod
    def from_raw(cls, raw: dict | None) -> DelayConfig:
        raw = coerce.as_mapping(raw)
        lo = coerce.as_float(raw.get("min_seconds"), 0.5)
        hi = coerce.as_float(raw.get("max_seconds"), 1.5)
        if hi < lo:  # 配置写反了也认，别让延迟变成负数
            lo, hi = hi, lo
        return cls(
            enable=coerce.as_bool(raw.get("enable"), True),
            min_seconds=max(0.0, lo),
            max_seconds=max(0.0, hi),
            sleep_extra_seconds=max(0.0, coerce.as_float(raw.get("sleep_extra_seconds"), 20.0)),
            activity_penalty=min(2.0, max(0.0, coerce.as_float(raw.get("activity_penalty"), 0.0))),
        )


def reply_delay_seconds(
    activity: float,
    *,
    config: DelayConfig | None = None,
    sleeping: bool = False,
    rng: random.Random | None = None,
) -> float:
    """算出这次回复该等多少秒。

    活跃度 1.0 → 落在 [min, max]；活跃度 0 → 落在 [min+(max−min), max+(max−min)]。
    睡眠时段（延迟关掉排队时才会走到）再加 sleep_extra_seconds。
    """
    cfg = config or DelayConfig()
    if not cfg.enable:
        return 0.0

    rand = rng or random.Random()
    activity = min(1.0, max(0.0, float(activity)))
    span = cfg.max_seconds - cfg.min_seconds
    base = rand.uniform(cfg.min_seconds, cfg.max_seconds)
    extra = (1.0 - activity) * span * cfg.activity_penalty
    total = base + extra + (cfg.sleep_extra_seconds if sleeping else 0.0)
    return max(0.0, total)
