"""Agent 接入抽象层（原型书第 19 节）。

设计要点不是"统一接口"，而是**承认 Agent 之间的差异**。三种 Agent 的形态差别很大：

  - **Claude Code**：一个长连接进程跑多轮，进程内权限拦截，本机历史会话存 JSONL
  - **Codex**：每轮一个独立进程（`codex exec` 跑完即退），靠 `exec resume <thread_id>`
    续接；非交互模式**根本没有权限提示**，权限由启动期的 sandbox 策略一次性决定
  - **OpenCode**：`opencode serve` 起的 HTTP 服务，事件走 SSE，历史会话要走 API 查

所以这一层只做三件事：

  1. 接口只表达**所有 Agent 都有**的东西
  2. 差异用 :class:`AgentCapabilities` **声明**，核心层按声明降级，而不是假设
  3. 具体协议细节（stream-json / thread_id / SSE）只允许出现在各自的实现模块里

**核心层（runner / api / ws）只 import 这个模块，永远不 import 具体 Adapter。**
新增一个 Agent = 新增一个子包 + 在 ``adapters/__init__.py`` 注册，核心层零改动。

接新 Agent 之前先读 ``docs/adapter-extension-notes.md``：里面记着 Codex 与 OpenCode
的调研结论，以及这套抽象**目前唯一已知的真实缺口**（agent 可能中途停下来要人批准，
而 :meth:`AgentSession.send` 现在假设它一定会返回）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from ..events import Event, EventType
from ..models import Project, Session, normalize_path, stable_id
from ..permissions import Policy

__all__ = [
    "AgentAdapter",
    "AgentCapabilities",
    "AgentInfo",
    "AgentSession",
    "AgentStatus",
    "AutonomyPolicy",
    "BlockFilter",
    "DEFAULT_AUTONOMY",
    "EmitFn",
    "RunOutcome",
    "ScanResult",
    "normalize_path",
    "stable_id",
]


class AgentStatus(str, Enum):
    """Agent 在这台机器上的可用状态。"""

    READY = "ready"
    """检测到且可用。"""

    UNCONFIGURED = "unconfigured"
    """装了但跑不起来（未登录、缺 API key 等）——列出来比静默隐藏有用。"""

    MISSING = "missing"


class AutonomyPolicy(str, Enum):
    """Agent 被允许自主到什么程度。

    **这是启动期决定，不是运行时拦截**，两种机制的区别在原型书第 22 节之外
    但很关键：先告诉 Agent 能做什么，和先让它做再拦，是完全不同的东西。

    之所以要提到抽象层：三种 Agent 表达这件事的机制完全不同
    （Claude 用 `--permission-mode`，Codex 用 `--sandbox`，OpenCode 用自己的配置），
    但"这个任务只许读不准写"是所有 Agent 都要回答的问题。
    """

    READ_ONLY = "read_only"
    """只给读类工具。结构上杜绝写操作，不依赖权限提示是否有人应答——
    因此它是**确定性**的，适合作为验收任务的档位。"""

    EDIT = "edit"
    """可以改文件，但不能执行任意命令。"""

    FULL = "full"
    """全部放行，只受绊线约束（第 6 层加入）。

    这是**默认档**：无人的 ``-p`` 模式下权限问题必然会被抛出来，"没人回答"
    等于"什么都干不了"，而用户要的是照常工作、别打扰。

    说清楚它**不是**什么：它不等于 ``--permission-mode bypassPermissions``，
    也不等于 ``--dangerously-skip-permissions``。那两个是把权限系统整个关掉，
    连"事后知道发生了什么"都没有。这一档相反——权限系统照常运转，
    每条绊线都必须**能落到一条事件上**，手机上看得见。

    实测依据见 ``probes/out/permissions_run2.txt``：用 ``--allowedTools``
    按名放行 + ``--disallowedTools`` 挂绊线，可以让写文件和跑命令都不被拦，
    同时让递归删除被真的拒掉（模型自报被拒，且没有绕路）。
    """


#: 用户不指定档位时用哪个。**只有一处定义，别处一律引用它。**
#:
#: 之所以要专门搞个常量：默认值散落在四处（HTTP 接口、runner、CLI、前端下拉框），
#: 而它们不一致时的表现是**手机上显示的是一个档、实际跑的是另一个档**——
#: 没有报错、没有日志，只有"怎么又写不了文件"这种说不清的抱怨。
#: 这个常量就是为了让"改默认值"变成一次修改，而不是四次。
#:
#: 内部签名（AgentSession / create_session）刻意**不用**它，仍然默认 READ_ONLY：
#: 那些是直接构造的入口，安全侧兜底比方便更重要，而它们总是被显式传参。
DEFAULT_AUTONOMY = AutonomyPolicy.FULL


class AgentCapabilities(BaseModel):
    """一个 Adapter 当前能提供什么。

    **语义是"本 Adapter 已具备"，不是"该 Agent 理论上支持"**——
    核心层拿它做功能降级判断，所以必须反映实际可用性。
    例如 Claude Code 底层有能力做权限拦截，但第 6 层才实现，这里就先标 False。
    """

    streaming_events: bool = False
    """能否输出结构化事件流。为 False 时只能拿到最终文本，UI 就没有工具调用视图。"""

    resume: bool = False
    """能否按 ID 续接已有会话。"""

    interrupt: bool = False
    """能否中断正在执行的任务。"""

    permission_gate: bool = False
    """能否在**运行时**拦下单个操作、等人批准后再继续。

    为 False 不代表"无法限制 Agent"，有两种不同的 False，别混为一谈：

      - **Codex**：非交互模式压根没有权限提示这个环节，只能用
        :class:`AutonomyPolicy` 一次性决定。
      - **Claude（当前）**：第 6 层的声明式管道能**拦**（``--disallowedTools``
        实测有效），但拦下来就是当场拒绝，**没法先问手机再决定**——
        那需要 ``--permission-prompt-tool`` 指到一个 MCP server 上，
        属于同一层的并行探针，还没验证。

    所以这里仍然标 False：等"问一下"真的能送达手机，它才会变成 True。
    标 True 而实际只能拒绝，会让上层以为可以弹审批框。
    """

    cost_reporting: bool = False
    """能否上报 token / 费用。"""

    history_discovery: bool = False
    """能否枚举本机历史项目与会话。

    OpenCode 没有本地会话文件，取历史要问它的 HTTP 服务，
    因此不是所有 Agent 都能响应"列出项目"。
    """


class AgentInfo(BaseModel):
    """一个 Agent 在**本机**的检测结果。"""

    id: str
    name: str
    status: AgentStatus
    version: str | None = None
    executable: str | None = None
    detail: str | None = None
    """未就绪时说明原因，直接展示给用户。"""

    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)

    @property
    def ready(self) -> bool:
        return self.status is AgentStatus.READY


class BlockFilter:
    """项目屏蔽过滤器。

    只做**精确匹配**，不做前缀匹配。原因：``C:\\Users\\lhy`` 是噪音，
    但它的子目录 ``C:\\Users\\lhy\\Desktop\\task_scheduler`` 是真实项目，
    前缀匹配会把后者一并误杀。
    """

    def __init__(self, blocked: Sequence[Path] = ()) -> None:
        self._blocked = {normalize_path(p) for p in blocked}

    def __call__(self, path: str) -> bool:
        return normalize_path(path) in self._blocked

    def __len__(self) -> int:
        return len(self._blocked)


@dataclass
class ScanResult:
    """一次项目发现的产出。

    刻意放在抽象层而不是某个 Adapter 里：这是**所有** Adapter 的返回类型，
    核心层读它时不该知道它来自哪个 Agent。
    """

    projects: list[Project] = field(default_factory=list)
    sessions: dict[str, list[Session]] = field(default_factory=dict)
    skipped_blocked: list[str] = field(default_factory=list)
    """被屏蔽清单挡掉的项目路径——列出来而不是静默丢弃，
    否则你会以为项目没被扫描到。"""

    @property
    def session_total(self) -> int:
        return sum(len(v) for v in self.sessions.values())


class RunOutcome(BaseModel):
    """一轮任务的结局。

    **刻意不用进程退出码**：实测 claude 任务成功时退出码也可能是 1，
    退出码反映的是 CLI 自身状态，不是任务成败。以 Agent 自报的终态为准。
    """

    ok: bool
    reason: str | None = None
    """Agent 自报的终态标识（Claude 是 result.subtype，Codex 是 turn.completed）。"""

    error: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    native_session_id: str | None = None
    """Agent 侧的会话句柄，续接时要用。"""


#: Adapter 往外吐事件的方式。同步、不阻塞——事件已经落库，慢消费者可以靠 seq 补发。
EmitFn = Callable[[Event], None]


class AgentSession(ABC):
    """一次持续的工作上下文。

    接口刻意设计成**每轮一次 send()**，而不是"给我一个事件流"，因为三种 Agent
    的进程模型完全不同：

      - Claude：一个常驻进程跑多轮（send 就是往 stdin 写一行）
      - Codex：每轮起一个新进程（send 就是跑 `codex exec resume <id>` 并等它退出）
      - OpenCode：HTTP 无状态（send 就是 POST 一次）

    "进程是不是常驻"因此降级成实现细节，上层不需要知道，也不该依赖。
    """

    def __init__(
        self,
        *,
        cwd: Path,
        task_id: str,
        emit: EmitFn,
        autonomy: AutonomyPolicy = AutonomyPolicy.READ_ONLY,
        resume_from: str | None = None,
    ) -> None:
        self.cwd = cwd
        self.task_id = task_id
        self.emit = emit
        self.autonomy = autonomy
        self.native_session_id: str | None = resume_from
        self._closed = False

    def _emit(self, type_: EventType, payload: dict) -> None:
        """把一条归一化事件交给上层。task_id 由这里统一填，Adapter 不必关心。"""
        self.emit(Event(task_id=self.task_id, type=type_, payload=payload))

    @abstractmethod
    async def send(self, prompt: str) -> RunOutcome:
        """跑一轮，阻塞到本轮结束。过程中事件通过 emit 实时流出。"""

    @abstractmethod
    async def interrupt(self) -> None:
        """请求中断当前轮。不保证立即生效，但要尽快。"""

    @abstractmethod
    async def close(self) -> None:
        """释放进程/连接。可重复调用。"""

    async def __aenter__(self) -> "AgentSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class AgentAdapter(ABC):
    """一个 Agent 的接入点。新增 Agent 只需实现这四个成员。"""

    agent_id: ClassVar[str]
    """稳定标识，用于落库与 API 路径，改了会破坏已有数据。"""

    display_name: ClassVar[str]

    @classmethod
    @abstractmethod
    def detect(cls) -> AgentInfo:
        """检测本机是否装了这个 Agent，以及它当前是否可用。

        必须**不抛异常**：没装是正常情况，用 AgentStatus.MISSING 表达。
        """

    @abstractmethod
    async def discover_projects(self, blocked: Sequence[Path] = ()) -> ScanResult:
        """枚举历史项目与会话。

        **async 是刻意的**：Claude / Codex 扫本地文件（同步返回也合规），
        但 OpenCode 要问它的 HTTP 服务。做成同步的话后者永远接不进来。

        不支持发现的 Adapter 返回空 ScanResult，并把
        ``capabilities.history_discovery`` 相应标 False，不要假装成功。
        """

    @abstractmethod
    def create_session(
        self,
        *,
        cwd: Path,
        task_id: str,
        emit: EmitFn,
        autonomy: AutonomyPolicy = AutonomyPolicy.READ_ONLY,
        resume_from: str | None = None,
        policy: Policy | None = None,
    ) -> AgentSession:
        """建一个会话句柄。**不应该在这里启动任何进程**——启动推迟到第一次 send()，
        这样任务在队列里等待时不会占着资源。

        ``policy`` 是可选的绊线清单，只有 :attr:`AutonomyPolicy.FULL` 档用得上。
        放在这里而不是让 Adapter 自己去读配置文件：**执行机制是 Adapter 的事，
        规则内容是核心层的事**。换成 Codex 时，同一份绊线要翻译成它的 sandbox 配置，
        规则本身不该跟着实现走。传 None 表示"用内置默认清单"。
        """
