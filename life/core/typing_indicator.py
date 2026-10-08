"""「正在输入」状态：等待期间并发续期，绝不泄漏任务、绝不影响回复。

## NapCat 能力边界（2026-10-08 实测 + 源码证据）

**支持私聊，不支持群聊。** 证据：

- NapCat 实现了 OneBot 动作 ``set_input_status``：
  ``/app/napcat/napcat.mjs:77549-77572``：payload schema 只有
  ``{user_id: "QQ号", event_type: 1}``，handler 里 ``chatType`` **硬编码**
  ``Te.KCHATTYPEC2C`` 并把 ``user_id`` 解析成好友 uid
  （``getUidByUinV2``）→ 只对私聊生效。
- 对照：支持群的同类动作会按 ``e.group_id`` 分支，例如
  ``napcat.mjs:76601-76603``；``set_input_status`` 完全没有这个分支。
- 实测（通过 NapCat WebUI 的 ``POST /api/Debug/call``）：
  - ``{"action":"set_input_status","params":{"user_id":"<管理员QQ>","event_type":1}}``
    → ``{"status":"ok","retcode":0,"data":{"result":0,"errMsg":"success"}}``（私聊可用）
  - 传 ``group_id`` → ``retcode 400 "Schema compilation error: Expected required property"``
  - ``{"action":"set_group_typing"}`` → ``不支持的 API: set_group_typing``（群聊没有这个 API）
  - ``user_id:"0"`` → ``retcode 200 "uid is empty"``（它按好友 uid 解析，进一步确认是 C2C）
- **无需开关**：``actionTags=["系统扩展"]``（``napcat.mjs:77558``）只用于 WebUI 列表展示
  （唯一引用点是 ``napcat.mjs:64894`` 的 ``tags: a.actionTags``），bundle 里找不到按
  tag / 配置禁用扩展 API 的代码；``napcat.json`` / ``onebot11*.json`` 里也没有相关开关。

所以 ``group`` 默认 **False**：NapCat 侧没有群聊「正在输入」能力，开了也只是白跑。

## 调用路径（为什么走 ``bot.call_action`` 而不是 HTTP）

NapCat 这边 ``httpServers`` / ``websocketServers`` 全是空的，它是以**反向 WS 客户端**
身份连到 AstrBot（``ws://astrbot:6199/ws``）；6099 是 NapCat WebUI 管理口，不是 OneBot 入口。
所以动作只能从 AstrBot 侧那条活着的连接发：``event.bot.call_action(...)``
（``core/platform/sources/aiocqhttp/aiocqhttp_message_event.py:33`` 有 ``self.bot = bot``，
同文件 ``:267`` / ``:291`` 就是现成用法）。

## 本模块的职责

- ``indicator()`` 是**异步上下文管理器**：进入时起一个续期任务（立刻发一次，然后每
  ``refresh_seconds`` 续一次），退出（正常 / 异常 / 被取消）时**一定**取消并 await 掉它。
- 任何失败（不支持 / 抛错 / 超时）都只记一条 debug 日志，并把这个会话记为「不支持」，
  之后不再调用——**绝不让它影响回复**。
- ``enable=false`` → 零副作用（不进循环、不建任务、不调用）。

纯逻辑：不 import astrbot，发送方（sender）从外面注入，便于单测。
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from . import coerce

TRIGGER_TIMEOUT_SECONDS = 5.0
"""单次「正在输入」调用的硬超时。★必须有：它只是装饰，绝不能把回复挂住。"""

MIN_REFRESH_SECONDS = 1.0
"""续期间隔下限：QQ 侧状态大约几秒过期，但更密的调用只会白白浪费 API。"""


@dataclass(frozen=True)
class TypingIndicatorConfig:
    """``typing_indicator`` 配置组。★默认关闭。"""

    enable: bool = False
    refresh_seconds: float = 4.0
    """「正在输入」会过期，需要定时续（Chrome/QQ 侧一般 5s 左右）。"""
    private: bool = True
    """私聊是否发（NapCat 实测支持）。"""
    group: bool = False
    """群聊是否发。**默认关**：NapCat 没有群聊「正在输入」API（见模块 docstring）。

    ⚠️ 硬把这项打开也不会得到「群里显示正在输入」：``set_input_status`` 只认 ``user_id``
    且走 C2C，所以效果是**给该群发言者发一个私聊**的「正在输入」——通常不是想要的。"""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> "TypingIndicatorConfig":
        data = coerce.as_mapping(raw)
        return cls(
            enable=coerce.as_bool(data.get("enable"), False),
            refresh_seconds=max(
                MIN_REFRESH_SECONDS, coerce.as_float(data.get("refresh_seconds"), 4.0)
            ),
            private=coerce.as_bool(data.get("private"), True),
            group=coerce.as_bool(data.get("group"), False),
        )

    def allows(self, is_private: bool) -> bool:
        """这个会话类型允许发「正在输入」吗。"""
        if not self.enable:
            return False
        return self.private if is_private else self.group

    def describe(self) -> str:
        return (
            f"正在输入：私聊={'on' if self.private else 'off'}"
            f" 群聊={'on' if self.group else 'off'}"
            f" 续期间隔={self.refresh_seconds:.1f}s"
        )


@dataclass(frozen=True)
class TypingTarget:
    """要给谁显示「正在输入」。"""

    umo: str
    is_private: bool
    user_id: str = ""
    """私聊对端 QQ（``set_input_status`` 的 ``user_id``）。"""


# sender(target) -> 协程；抛异常 / 超时都算「这个会话不支持」
Sender = Callable[[TypingTarget], Awaitable[Any]]


class TypingIndicator:
    """按会话管理续期任务。线程/事件循环内共用一份即可（``unsupported`` 会记在实例上）。"""

    def __init__(
        self,
        config: TypingIndicatorConfig | None = None,
        *,
        logger: Any = None,
        timeout: float = TRIGGER_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config or TypingIndicatorConfig()
        self._logger = logger
        self._timeout = max(0.1, float(timeout))
        self._unsupported: set[str] = set()

    @property
    def unsupported(self) -> frozenset[str]:
        """已经判定「不支持 / 一直失败」的会话（umo），这些以后不再调用。"""
        return frozenset(self._unsupported)

    def reset_unsupported(self) -> None:
        self._unsupported.clear()

    def _log(self, level: str, message: str, *args) -> None:
        if self._logger is None:
            return
        getattr(self._logger, level, self._logger.debug)(message, *args)

    @asynccontextmanager
    async def indicator(self, target: TypingTarget, sender: Sender):
        """在 ``async with`` 块内持续续期「正在输入」。

        - ``enable=false`` / 该会话类型没开 / 已知不支持 → 直接 yield，**零副作用**
        - 退出时（正常、抛异常、被取消）一定取消并 await 掉续期任务，绝不泄漏
        """
        if not self.config.allows(target.is_private) or target.umo in self._unsupported:
            yield
            return

        task = asyncio.get_running_loop().create_task(
            self._refresh_loop(target, sender), name="life-typing-indicator"
        )
        try:
            yield
        finally:
            # ★绝不泄漏任务：无论正常结束、异常还是被取消，都要收干净。
            #   这里吞掉 CancelledError 是**只针对子任务**的；外层协程自己的取消
            #   会在这个 finally 之后继续向上抛，不会被这里吃掉。
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _refresh_loop(self, target: TypingTarget, sender: Sender) -> None:
        while True:
            await self._trigger(target, sender)
            await asyncio.sleep(self.config.refresh_seconds)

    async def _trigger(self, target: TypingTarget, sender: Sender) -> None:
        """发一次状态。**绝不抛异常**（除了自身的取消）。"""
        if target.umo in self._unsupported:
            return
        try:
            await asyncio.wait_for(sender(target), timeout=self._timeout)
        except asyncio.CancelledError:
            raise  # 关任务时的正常路径，继续往上传
        except Exception as exc:
            # 记一条 debug（只此一次：进 _unsupported 后不再调用），然后彻底停手
            self._unsupported.add(target.umo)
            self._log(
                "debug",
                "[life] 正在输入：该会话不可用，已停用后续调用 session=%s err=%r",
                target.umo,
                exc,
            )
