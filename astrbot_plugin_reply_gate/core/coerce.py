"""配置值的容错转换。

AstrBot 的配置面板是给人填的，填错是常态：把 ``30`` 写成 ``abc``、把布尔勾成字符串 ``"false"``。
如果直接 ``int(raw["x"])``，一次手滑就让整个插件在加载时炸掉——那种崩法最难查。

所以**所有**从配置 dict 取值的地方都走这里：拿不到就退回默认值，绝不抛异常。
只有 ``time_weights`` 例外（schedule.py 会直接报错），因为作息表填错必须吵，
但 main.py 会捕获它并用默认表兜底 + 打一条 error 日志。
"""
from __future__ import annotations

from typing import Any, Mapping

_TRUE = {"true", "1", "yes", "y", "on", "是", "开"}
_FALSE = {"false", "0", "no", "n", "off", "否", "关", ""}


def as_float(value: Any, default: float = 0.0) -> float:
    """转 float。失败 → default。布尔会被当成 1/0（面板有时给 bool）。"""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    # 过滤 NaN / inf：它们会一路污染概率和延迟计算
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def as_int(value: Any, default: int = 0) -> int:
    """转 int。失败 → default。浮点会截断（配置里写 2.0 是合理的）。"""
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    """转 bool。字符串按 "true/false/1/0/是/否" 认，认不出 → default。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return default


def as_mapping(value: Any) -> Mapping[str, Any]:
    """确保拿到的是 dict，否则给个空 dict（方便 .get 链式调用）。"""
    return value if isinstance(value, Mapping) else {}


def as_id_list(value: Any) -> frozenset[str]:
    """QQ 号名单：接受 list，也接受换行/逗号分隔的文本。去空白、丢空项。"""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        items = value.replace(",", "\n").replace("，", "\n").splitlines()
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = [value]
    out = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        # 面板里可能存成 12345.0
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]
        out.add(text)
    return frozenset(out)
