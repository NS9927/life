"""把 ``astrbot`` 相关模块桩掉，好在没有 AstrBot 环境时也能验证 main.py 的接线。

只桩 main.py 真正用到的那几个符号：logger、AstrMessageEvent、filter、
Context/Star/register、Plain、MessageChain。桩是**照着真实源码的用法**写的
（`MessageChain(chain=[...])`、`filter.event_message_type(..., priority=n)`、
`context.get_config(umo=...)`），签名对不上就说明接线错了。

**这不是 AstrBot 的替代品**：桩只能证明「我们的调用姿势和真实 API 一致」，
证明不了运行时行为。运行时行为要在 WSL 里的真容器上验。
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field


class FakeLogger:
    """记录所有日志，方便断言「已读不回」真的打了日志。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, msg, *args) -> None:
        try:
            text = msg % args if args else str(msg)
        except Exception:
            text = f"{msg} {args}"
        self.records.append((level, text))

    def debug(self, msg, *args) -> None:
        self._log("debug", msg, *args)

    def info(self, msg, *args) -> None:
        self._log("info", msg, *args)

    def warning(self, msg, *args) -> None:
        self._log("warning", msg, *args)

    def error(self, msg, *args) -> None:
        self._log("error", msg, *args)

    def exception(self, msg, *args) -> None:
        self._log("exception", msg, *args)

    def messages(self, level: str | None = None) -> list[str]:
        return [text for lvl, text in self.records if level is None or lvl == level]


@dataclass
class FakePlain:
    text: str


@dataclass
class FakeMessageChain:
    chain: list = field(default_factory=list)


class FakeEventMessageType:
    GROUP_MESSAGE = 1
    PRIVATE_MESSAGE = 2
    OTHER_MESSAGE = 4
    ALL = 7


def _passthrough_decorator(*_args, **_kwargs):
    def wrapper(func):
        func._registered = True
        return func

    return wrapper


class FakeStar:
    def __init__(self, context) -> None:
        self.context = context


class FakeContext:
    """插件拿到的 context。页面接口靠 register_web_api 注册，这里记下来方便断言。"""

    def __init__(self) -> None:
        self.registered_web_apis: list[tuple] = []

    def register_web_api(self, route, view_handler, methods, desc) -> None:
        self.registered_web_apis = [r for r in self.registered_web_apis if r[0] != route]
        self.registered_web_apis.append((route, view_handler, methods, desc))


class FakeRequest:
    """假 request 代理（对应 astrbot.api.web.request）。

    测试里直接给 ``payload`` / ``query_params`` 赋值，handler 就能读到。
    """

    def __init__(self) -> None:
        self.payload: dict = {}
        self.query_params: dict = {}

    async def json(self, default=None):
        return self.payload if self.payload is not None else default

    @property
    def query(self):
        return self.query_params

    @property
    def method(self) -> str:
        return "POST"

    @property
    def username(self) -> str:
        return "tester"


web_request = FakeRequest()


def fake_register(*_args, **_kwargs):
    def wrapper(cls):
        return cls

    return wrapper


def install() -> FakeLogger:
    """装好桩模块。必须在 import main.py **之前**调用。

    可重复调用（幂等）：已装过就直接返回同一个 logger。
    """
    existing = sys.modules.get("astrbot")
    if existing is not None and hasattr(existing, "_reply_gate_stub"):
        return existing.logger

    logger = FakeLogger()

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []

    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = logger

    filter_mod = types.ModuleType("astrbot.api.event.filter")
    filter_mod.EventMessageType = FakeEventMessageType
    filter_mod.event_message_type = _passthrough_decorator
    filter_mod.on_decorating_result = _passthrough_decorator
    filter_mod.after_message_sent = _passthrough_decorator
    filter_mod.platform_adapter_type = _passthrough_decorator
    filter_mod.command = _passthrough_decorator

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.__path__ = []
    event_mod.filter = filter_mod

    class AstrMessageEvent:  # noqa: N801 - 与真实类同名
        pass

    event_mod.AstrMessageEvent = AstrMessageEvent

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.__path__ = []
    star_mod.Context = FakeContext
    star_mod.Star = FakeStar
    star_mod.register = fake_register

    api.event = event_mod
    api.star = star_mod

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.request = web_request
    api.web = web_mod

    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    message = types.ModuleType("astrbot.core.message")
    message.__path__ = []
    components = types.ModuleType("astrbot.core.message.components")
    components.Plain = FakePlain
    message_result = types.ModuleType("astrbot.core.message.message_event_result")
    message_result.MessageChain = FakeMessageChain

    astrbot.api = api
    astrbot.logger = logger  # install() 第二次调用时会 return existing.logger

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.star": star_mod,
            "astrbot.api.web": web_mod,
            "astrbot.core": core,
            "astrbot.core.message": message,
            "astrbot.core.message.components": components,
            "astrbot.core.message.message_event_result": message_result,
        }
    )
    astrbot._reply_gate_stub = True  # type: ignore[attr-defined]
    return logger
