"""消息归类：把 AstrBot 事件的几个标志位压成三种语义之一。

设计依据：docs/可行性报告与设计.md 第二节。**这一层做错，整个插件就废了**——
把「被 @」当成「群聊闲聊」去掷骰子，机器人就会在被人喊的时候随机装死。

AstrBot 侧的判据（已核对源码）：
- ``event.is_at_or_wake_command``：WakingCheckStage 里被置 True 的场景是
  唤醒前缀命中 / 被 @ / 被 @全体 / 引用了机器人 / **私聊**（waking_check/stage.py:128,149,158）
- ``event.is_private_chat()``：``get_message_type() == MessageType.FRIEND_MESSAGE``
  （astr_message_event.py:260）

私聊其实已经被 ``is_at_or_wake_command`` 覆盖了，但仍然两个都看：
``friend_message_needs_wake_prefix`` 打开时私聊不会被置位，那时得靠 ``is_private_chat()``。

纯函数，不 import astrbot，所以能单测。
"""
from __future__ import annotations

from .gate import MessageKind


def classify(*, is_private: bool, at_or_wake: bool) -> MessageKind:
    """判定消息语义。

    - 私聊 / 被 @ / 命中唤醒词 / 引用了机器人 → ADDRESSED（绝不用概率）
    - 其余群聊消息 → CHIME（可以按活跃度概率放行）

    主动开口（PROACTIVE）不由事件产生，走自己的调度器，见 README 待办。
    """
    if is_private or at_or_wake:
        return MessageKind.ADDRESSED
    return MessageKind.CHIME


def is_self_message(sender_id: str, self_id: str) -> bool:
    """是不是机器人自己发的消息。两个 id 都非空且相等才算，避免空串误判。"""
    return bool(sender_id) and bool(self_id) and sender_id == self_id
