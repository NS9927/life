"""classify.py 单测：事件标志位 → 消息语义。这一层错了整个插件就废了。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "life"))

from core.classify import classify, is_self_message  # noqa: E402
from core.gate import MessageKind  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_group_chatter_is_chime(self):
        self.assertIs(classify(is_private=False, at_or_wake=False), MessageKind.CHIME)

    def test_at_bot_is_addressed(self):
        self.assertIs(classify(is_private=False, at_or_wake=True), MessageKind.ADDRESSED)

    def test_private_is_addressed(self):
        # friend_message_needs_wake_prefix 打开时私聊不会被置 at_or_wake，必须靠这一条
        self.assertIs(classify(is_private=True, at_or_wake=False), MessageKind.ADDRESSED)

    def test_private_and_at_is_addressed(self):
        self.assertIs(classify(is_private=True, at_or_wake=True), MessageKind.ADDRESSED)


class TestSelfMessage(unittest.TestCase):
    def test_same_id(self):
        self.assertTrue(is_self_message("9999", "9999"))

    def test_different_id(self):
        self.assertFalse(is_self_message("2001", "9999"))

    def test_empty_ids_never_match(self):
        # AstrBot 拿不到 id 时会返回空字符串，空 == 空 不能算自己发的
        self.assertFalse(is_self_message("", ""))
        self.assertFalse(is_self_message("", "9999"))
        self.assertFalse(is_self_message("9999", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
