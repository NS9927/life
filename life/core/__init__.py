"""回复闸门的核心模块。

模块划分（对应设计文档第四节）：

    schedule.py   时段权重表 → 0~1 活跃度（切点插值 + 周末系数）。纯函数
    classify.py   事件标志位 → 消息语义（被点名 / 群聊插话）。纯函数
    gate.py       判定链：静默名单 → 睡眠 → 熔断 → 冷却 → 概率 → 连续丢弃兜底
    batch.py      低活跃度时「攒一波统一回」的调度。纯逻辑
    queue.py      睡眠期点名消息队列 + 起床补发文案 + 定时判定。纯数据结构
    delay.py      按作息的回复延迟。纯函数
    coerce.py     配置值容错转换（配置填错不许把插件搞崩）

**core 里的模块一律不 import astrbot**：时间、随机数从外面注入。
所以 `python -m unittest discover -s tests` 不需要 AstrBot 环境就能跑。

main.py 只做事件适配与钩子注册，判定规则一条都不写在里面。
"""
from . import batch, classify, coerce, delay, gate, queue, schedule  # noqa: F401

__all__ = ["batch", "classify", "coerce", "delay", "gate", "queue", "schedule"]
