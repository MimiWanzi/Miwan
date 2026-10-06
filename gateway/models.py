"""核心实体模型（对应原型书第 10 节）。

实体关系：Project → Session → Task → Event
"""

from __future__ import annotations

import os
from enum import Enum
from hashlib import sha1
from pathlib import Path

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务状态机（原型书第 14 节）。

    正常路径：
        created → queued → running → completed

    waiting_approval 只在权限策略设为「问我」时才会出现；默认放行策略下不经过它。
    """

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DISCONNECTED = "disconnected"


#: 终态集合——进入其中任一状态后任务不再变化
TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.DISCONNECTED,
    }
)


# ----------------------------------------------------------------------
# 项目身份
#
# 放在领域层而不是适配层：**项目身份 = 路径** 是一条业务规则，不是某个 Agent
# 的实现细节。同一个目录无论被哪个 Agent 用过都是同一个项目，
# 所以 id 由路径派生，而不是由"谁发现的"派生。
# ----------------------------------------------------------------------


def stable_id(text: str) -> str:
    """由内容派生的稳定 ID。同一个路径每次算出来都一样。"""
    return sha1(text.encode("utf-8")).hexdigest()[:12]


def normalize_path(path: str | Path) -> str:
    """规范化路径用于比较：绝对化 + 大小写归一。

    Windows 路径不区分大小写、分隔符还可能是正反斜杠混用，
    直接做字符串相等会漏掉本该匹配的项目。
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return os.path.normcase(str(resolved))


class Project(BaseModel):
    """Agent 工作的项目。"""

    id: str
    name: str
    path: str
    git_branch: str | None = None
    last_active_at: float | None = None
    session_count: int = 0

    manual: bool = False
    """用户手动添加的项目（扫描永远发现不了它——目录还没被 Agent 用过，
    Agent 存储里就没有它的记录）。区别只有一个实际后果：
    刷新时的清理不能删它，否则第一次刷新它就消失了。"""


class Session(BaseModel):
    """Agent 的一次持续工作上下文。"""

    id: str
    project_id: str
    agent_id: str = ""
    """这个会话属于哪个 Agent。同一个目录可能被多个 Agent 用过，
    而会话是**某个 Agent 的**——续接时必须回到原来那个，
    拿 Claude 的 session id 去喂 Codex 只会得到一个查无此会话。"""

    native_session_id: str | None = None
    """Agent 侧的会话句柄——续接时要用它。
    对 Claude Code 是 session uuid，对 Codex 是 thread_id。"""

    title: str = ""
    git_branch: str | None = None
    created_at: float | None = None
    last_active_at: float | None = None


class Task(BaseModel):
    """用户交给 Agent 的一个具体工作任务。"""

    id: str
    project_id: str
    agent_id: str = ""
    session_id: str | None = None
    native_session_id: str | None = None
    """Agent 侧的会话句柄，落库是为了**进程重启后还能续接**。
    不存的话，网关一崩，那些断掉的任务就永远接不回去了。"""

    prompt: str
    status: TaskStatus = TaskStatus.CREATED
    created_at: float = Field(default_factory=lambda: 0.0)
    started_at: float | None = None
    finished_at: float | None = None
    exit_reason: str | None = None
    error: str | None = None
    cost_usd: float | None = None
