"""应用上下文：把 store / bus / runner / config 捆在一起，供 HTTP 层取用。

单独成模块的唯一原因是**打断循环 import**：``server`` 要装配 ``api`` 与 ``ws``，
而 ``api`` 与 ``ws`` 又得拿到上下文。放在 ``server`` 里立刻成环。

用 ``web.AppKey`` 而不是字符串 ``"ctx"``：写错名字时前者在取用那一刻就报
"AppKey 未设置"，后者是一个非常容易被 try/except 吞掉的 KeyError。
"""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web

from .auth import AuthService
from .config import Config
from .events import EventBus
from .notify import Notifier
from .runner import TaskRunner
from .store import Store


@dataclass
class GatewayContext:
    """一个 Gateway 进程里的全部共享状态。

    刻意只是一个数据袋：它不提供任何行为，所以不会长成一个"什么都往里塞"的上帝对象。
    需要「扫一遍项目」这类跨模块动作时，写进 ``sync`` 那种专门的模块里。
    """

    config: Config
    store: Store
    bus: EventBus
    runner: TaskRunner
    auth: AuthService
    #: 微信推送。挂在上下文上（而不是 runner 里）是因为设置页要读改它的设置。
    notifier: Notifier


CTX: web.AppKey[GatewayContext] = web.AppKey("gateway_ctx", GatewayContext)

#: 当前请求的 :class:`~gateway.auth.Device`，由 ``server`` 的鉴权中间件挂上。
#:
#: 放在这里而不是 ``server`` 里，是为了让 ``api`` 能取到它而不 import ``server``：
#: ``server`` 要装配 ``api``，反过来 import 就成环了（这正是本模块存在的理由）。
#: 挂在 **request** 上而不是 app 上——app 是全局的，一个请求的身份写进去
#: 会污染另一个请求。
DEVICE_KEY: web.AppKey = web.AppKey("device", object)


def get_ctx(request: web.Request) -> GatewayContext:
    return request.app[CTX]


def get_device(request: web.Request):
    """当前请求背后的设备；未认证时为 None。"""
    return request.get(DEVICE_KEY)
