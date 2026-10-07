"""延迟埋点的**纯逻辑**：记录构造、jsonl 编码、超行截断、引用 id 提取。

背景（真机案例）：一条回复在 21:53:07 发出，却引用了 21:47:25 的消息——滞后 342 秒。
闸门判定只花 20~100ms、我们加的延迟上限 1.5s、那段时间也没走攒批，
所以 342 秒发生在我们「放行」之后到 ``respond.stage`` 之间（AstrBot 流水线 / 别的插件）。
埋点就是把这段拆开：``t_recv``（平台发出）→ ``t_recv_wall``（我们开始处理）
→ ``t_gate`` / ``t_allow``（我们判定）→ ``t_decorate``（进入装饰阶段）→ ``t_ready``（延时结束）。

本模块**不 import astrbot、不做任何 I/O**（I/O 在 main.py 的 ``TimingSink`` 里）：
- 时间戳、字段值全部由调用方注入，所以能直接单测；
- 同一个输入必然得到同一行 JSON。

字段约定见 ``TIMING_FIELDS``。取不到的字段一律 ``None``（JSON 里是 ``null``）——
埋点只有「有数据 / 没数据」两态，**绝不因为取不到字段而报错**。
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

# 一行记录的字段全集（顺序固定，方便肉眼比对和 diff）。
TIMING_FIELDS: tuple[str, ...] = (
    # 消息在平台上发出的时间（epoch 秒）：优先原始事件的 time（真正的平台发出时间），
    # 适配器不给才退化成 AstrBot 的接收/转换时间；都拿不到 → null
    "t_recv",
    "t_recv_wall",  # 我们开始处理这条事件的时间（和 t_recv 相减 = 平台侧积压）
    "t_gate",  # 我们做完闸门判定的时间
    "t_allow",  # 判定为放行 / 入队的时间；DROP 没有 → null
    "t_decorate",  # 进入 on_decorating_result 的时间（只有真要走发送的消息才记）
    "t_ready",  # 延时 sleep 结束、准备交给 AstrBot 发送的时间
    "delay",  # 我们那次实际 sleep 的秒数（0 = 没睡；攒批等待记在 batch.wait）
    "umo",  # 会话
    "sender_id",
    "kind",  # addressed / chime / proactive
    "action",  # allow / drop / queue
    "reason",  # gate.py 里的判定原因常量
    "prob",  # 这次掷骰子的概率
    "activity",  # 时段活跃度 0~1
    "message_id",  # 平台消息 id
    "quoted_id",  # 消息链里 Reply 组件引用的消息 id
    "batch",  # 攒批：{"lead": bool, "wait": N, "merged": M}；普通路径 null
)

# 文件上限：超过就清空重写（保留最近的部分），避免长期开着把磁盘写满。
MAX_LINES = 2000
MAX_BYTES = 512 * 1024


def normalize_record(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """把任意 dict 规整成「字段全集 + 缺的填 None」。

    未知键直接丢掉：一行就是固定 schema，多出来的键只会让人怀疑是不是写错了地方。
    """
    source = record if isinstance(record, Mapping) else {}
    return {name: source.get(name) for name in TIMING_FIELDS}


def encode_record(record: Mapping[str, Any] | None) -> str:
    """记录 → 一行 JSON（不带换行符）。

    ``ensure_ascii=False``：中文必须原样落盘，否则排障时没法直接看。
    ``default=str``：万一把 Action/MessageKind 之外的东西塞进来，也不要抛。
    """
    return json.dumps(
        normalize_record(record),
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


# ----------------------------------------------------------------------
# 文件体积控制
# ----------------------------------------------------------------------
def needs_rotate(
    line_count: int,
    size_bytes: int,
    *,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
) -> bool:
    """写入前判断要不要清空重写。行数或字节数任一超限就重写。"""
    return int(line_count) >= int(max_lines) or int(size_bytes) >= int(max_bytes)


def decode_lines(text: str) -> list[str]:
    """文件正文 → 非空行列表（去掉行尾换行和空行）。"""
    return [line for line in (text or "").splitlines() if line.strip()]


def dump_lines(lines: Sequence[str]) -> str:
    """行列表 → 文件正文（每行一个 ``\\n``）。"""
    return "".join(f"{line}\n" for line in lines)


def trim_lines(
    lines: Sequence[str],
    *,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
    keep_ratio: float = 0.5,
) -> list[str]:
    """超限时只保留**最新**的一部分。

    为什么要留一半而不是留空：如果清空后立刻又被写满，每条消息都要重写整个文件，
    反而变成同步大 I/O。留最近的一半，下一次重写要再攒够一半的量。
    """
    keep_lines = max(1, int(int(max_lines) * float(keep_ratio)))
    keep_bytes = max(1, int(int(max_bytes) * float(keep_ratio)))

    kept: list[str] = []
    total = 0
    for line in reversed(list(lines)):
        size = len(line.encode("utf-8")) + 1  # +1 = 换行符
        if kept and (len(kept) >= keep_lines or total + size > keep_bytes):
            break
        kept.append(line)
        total += size
    kept.reverse()
    return kept


# ----------------------------------------------------------------------
# 引用消息 id
# ----------------------------------------------------------------------
def _is_reply_component(component: Any) -> bool:
    """判断是不是 Reply 组件。按鸭子类型认，不 import astrbot（core 不许依赖框架）。"""
    if isinstance(component, Mapping):
        return str(component.get("type", "")).strip().lower() == "reply"
    if type(component).__name__ == "Reply":
        return True
    return str(getattr(component, "type", "")).strip().lower() == "reply"


def quoted_id_from_message_chain(chain: Any) -> str:
    """从消息链里取被引用消息的 id（没有 Reply 组件就拿不到）。

    AstrBot 的 Reply 组件字段是 ``id``；有的适配器只给 ``message_id``，
    所以两个都试。任何异常 / 取不到都返回空串，绝不上抛。
    """
    try:
        components = list(chain or [])
    except Exception:
        return ""
    for component in components:
        if not _is_reply_component(component):
            continue
        for attr in ("id", "message_id"):
            if isinstance(component, Mapping):
                value = component.get(attr)
            else:
                try:
                    value = getattr(component, attr, None)
                except Exception:
                    value = None
            if value not in (None, ""):
                return str(value)
    return ""
