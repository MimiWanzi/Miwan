"""归一化事件类型与进程内事件总线。

Adapter 层的核心职责是**归一化**：把各 Agent 互不相同的原始输出映射成同一套事件。
不做归一化的话，前端会被具体 Agent 的格式污染，加第二个 Agent 就会崩。

详见 probes/out/ 下的真实样本；类型定义来自对 Claude Code 实测的观察。
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """MVP-0 的最小事件集。

    后续可扩充原型书第 15 节的 file_read / file_write / test_started 等——
    多数可由 TOOL_CALL 加上工具名派生，不必让 Adapter 单独上报。
    """

    TASK_STARTED = "task_started"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DECIDED = "permission_decided"
    """一条待批准的权限请求有了结果。payload 带 permission_id，
    前端据此把对应的「等你批准」卡片换成结果。"""
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    RAW = "raw"
    """兜底：Adapter 认不出的原始事件原样透传，保证信息不丢。"""


class Event(BaseModel):
    seq: int = 0
    """持久化后由 store 回填的单调递增序号。事件在总线上流转时仍是 0。"""

    task_id: str
    type: EventType
    ts: float = Field(default_factory=time.time)
    payload: dict = Field(default_factory=dict)


class EventBus:
    """进程内发布订阅。

    刻意保持「只发不管」：发布方不知道有几个订阅者，也不知道有没有 WebSocket 存在。
    这条边界是把局域网换成云隧道时业务层不用改的前提。

    同步 SQLite + 同步 publish 意味着发布不会 await；这对低事件量是合适的。
    若将来事件量级上来了，这里换 asyncio.Queue + 单独的分发协程即可，接口不变。
    """

    def __init__(self, queue_size: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: Event) -> None:
        """同步投递，绝不阻塞。

        慢订阅者不允许拖住 Agent 的执行——队列满了就丢最旧的。这不是丢数据：
        事件已经落库，慢客户端重连时靠 seq 补发就能追回来。
        """
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
