"""任务生命周期状态机（原型书第 14 节）。

    created → running → completed / failed / cancelled / disconnected

这个模块是「业务层」的代表：它只依赖 :class:`~gateway.store.Store` 和
:class:`~gateway.events.EventBus`，**不 import 任何 aiohttp / WebSocket 的东西**。
把局域网直连换成云隧道时，这里零改动。

三条约束来自原型书第 21 节「断线恢复」，不是为将来上云额外加的成本：

  1. 任务状态落盘 —— 进程重启后知道发生过什么
  2. 事件带单调递增 seq 持久化 —— 重连时从 last_seq 补发
  3. 业务层不感知传输 —— 事件只发给 EventBus，谁在听、有几个在听，这里不知道
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from .adapters import DEFAULT_AUTONOMY, AutonomyPolicy, EmitFn, RunOutcome, get
from .adapters.base import AgentSession
from .events import Event, EventBus, EventType
from .models import TERMINAL_STATUSES, Task, TaskStatus
from .permissions import Policy
from .store import Store


class RunnerError(RuntimeError):
    """提交任务时的前置条件不满足。"""


class UnknownAgent(RunnerError):
    pass


class UnknownProject(RunnerError):
    pass


def status_for(outcome: RunOutcome) -> TaskStatus:
    """把 Adapter 报的结局翻译成任务状态。

    ``cancelled`` 单独判一次：它和 failed 都是 ok=False，但对用户含义完全相反——
    一个是"我按了取消"，一个是"出错了"。
    """
    if outcome.ok:
        return TaskStatus.COMPLETED
    if outcome.reason == "cancelled":
        return TaskStatus.CANCELLED
    return TaskStatus.FAILED


class TaskRunner:
    """提交、驱动、取消任务。"""

    def __init__(
        self,
        store: Store,
        bus: EventBus,
        default_agent: str = "claude_code",
        policy: Policy | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.default_agent = default_agent
        #: 绊线清单。只有 FULL 档用得上，但放在 runner 上而不是 api 上——
        #: 它是**配置**，一份进程一份，不该每个请求重新读一次文件
        #: （读文件本身不贵，贵的是"改了文件但没生效"这种要靠猜的 bug）。
        #: 同样判 `is None`：Policy 有 __len__，空清单是假值。
        self.policy = Policy() if policy is None else policy
        self._running: dict[str, asyncio.Task[None]] = {}
        self._sessions: dict[str, AgentSession] = {}

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------

    def submit(
        self,
        *,
        project_id: str,
        prompt: str,
        agent_id: str | None = None,
        autonomy: AutonomyPolicy = DEFAULT_AUTONOMY,
        resume_from: str | None = None,
        session_id: str | None = None,
    ) -> Task:
        """建好任务并**立即返回**，不阻塞到任务结束。

        这是"手机下完命令马上能走"的前提，也是断线恢复的前提：
        如果 submit 一直阻塞到任务跑完，客户端一断线就什么都拿不到了。
        """
        agent_id = agent_id or self.default_agent
        adapter_cls = get(agent_id)
        if adapter_cls is None:
            raise UnknownAgent(f"未注册的 Agent：{agent_id}")

        project = self.store.get_project(project_id)
        if project is None:
            raise UnknownProject(f"库里没有这个项目：{project_id}")

        task = Task(
            id=f"t-{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            agent_id=agent_id,
            session_id=session_id,
            prompt=prompt,
            status=TaskStatus.CREATED,
            created_at=time.time(),
        )
        self.store.create_task(task)

        runner = asyncio.create_task(
            self._execute(task, adapter_cls, Path(project.path), autonomy, resume_from)
        )
        self._running[task.id] = runner
        return task

    async def _execute(
        self,
        task: Task,
        adapter_cls,
        cwd: Path,
        autonomy: AutonomyPolicy,
        resume_from: str | None,
    ) -> None:
        sink = self._sink(task.id)

        try:
            session = adapter_cls().create_session(
                cwd=cwd,
                task_id=task.id,
                emit=sink,
                autonomy=autonomy,
                resume_from=resume_from,
                policy=self.policy,
            )
        except Exception as exc:  # noqa: BLE001 —— 建会话失败不能让协程带着异常消失
            self._finish(task.id, TaskStatus.FAILED, f"创建会话失败：{exc}")
            self._running.pop(task.id, None)
            return

        self._sessions[task.id] = session

        # 会话一建出来就知道 native_session_id，**立刻落库**：
        # 万一网关在这之后崩掉，手机上还能看到"断在哪个会话上"，可以续接。
        # 等到任务跑完再存是不够的——崩掉的那一刻恰恰是任务没跑完。
        self.store.update_task(task.id, native_session_id=session.native_session_id)
        self.store.update_task(
            task.id, status=TaskStatus.RUNNING, started_at=time.time()
        )

        try:
            outcome = await session.send(task.prompt)
        except asyncio.CancelledError:
            # 网关自己在关闭，任务被强行掐断。
            # exit_reason 和 recover() 的 gateway_restart 对齐：手机上问
            # "这个任务为什么被取消了"，得有一个能区分的答案，
            # 不能和"我自己按了取消"混成同一个 cancelled。
            self._finish(
                task.id,
                TaskStatus.CANCELLED,
                "网关关闭，任务被中断",
                exit_reason="gateway_shutdown",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            self._finish(task.id, TaskStatus.FAILED, f"{type(exc).__name__}: {exc}")
        else:
            self._finish(
                task.id,
                status_for(outcome),
                outcome.error,
                exit_reason=outcome.reason,
                cost_usd=outcome.cost_usd,
                native_session_id=outcome.native_session_id,
            )
        finally:
            self._sessions.pop(task.id, None)
            self._running.pop(task.id, None)
            await session.close()

    def _sink(self, task_id: str) -> EmitFn:
        """Adapter 吐事件的地方——**持久化与广播的唯一汇合点**。

        顺序很重要：**先落库拿到 seq，再广播**。反过来的话，订阅者会先收到一个
        seq=0 的事件，而它其实已经在库里了——客户端按 seq 去重时反而会把这条
        当成"还没有的"再收一遍。seq 必须在广播出门前就是最终值。
        """

        def emit(event: Event) -> None:
            self.store.append_event(event)  # 就地回填 event.seq
            self.bus.publish(event)

        return emit

    def _finish(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None = None,
        *,
        exit_reason: str | None = None,
        cost_usd: float | None = None,
        native_session_id: str | None = None,
    ) -> None:
        """写终态。**所有终态写入都走这里**——分散着写迟早会漏掉 finished_at，
        而漏掉之后任务在 UI 上会永远停在"运行中"。
        """
        fields: dict = {
            "status": status,
            "finished_at": time.time(),
            "error": error,
        }
        if exit_reason is not None:
            fields["exit_reason"] = exit_reason
        if cost_usd is not None:
            fields["cost_usd"] = cost_usd
        if native_session_id is not None:
            fields["native_session_id"] = native_session_id
        self.store.update_task(task_id, **fields)
        self._ensure_terminal_event(task_id, status, error, exit_reason, cost_usd)

    def _ensure_terminal_event(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None,
        exit_reason: str | None,
        cost_usd: float | None,
    ) -> None:
        """保证走到终态的任务，事件流里都有一个终点。

        **状态有两个去处**：``tasks`` 表（权威）和事件流（给人看的叙事）。
        手机重连时两个都拿得到，所以只看 tasks 表的那条路一直是对的——
        这也是为什么这个洞藏得住。但 ``ws.py`` 明确承诺"WebSocket 只是优化，
        客户端只靠 ``GET /api/tasks/{id}/events`` 轮询也能用"，那条路只有事件流；
        流里没有终点，轮询的客户端会永远以为任务还在跑。

        适配器在**大多数**路径上自己会发 TASK_COMPLETED / TASK_FAILED
        （``claude_code/session.py`` 的每条失败分支都发了），那种情况这里
        什么都不做——再补一条会让事件流出现两个终点。

        缺口在于适配器**根本没被走到**的那几条：
        ``create_session`` 抛异常、``send()`` 抛了它没预料的异常、
        以及网关关停时整个协程被取消。``recover()`` 里为"网关重启"
        专门补过一条事件，理由写得很清楚——"否则回放会看到任务停在 running
        就没了，像是数据丢了"。这三条是同一个问题的漏网之鱼。
        """
        if status not in TERMINAL_STATUSES:
            return
        if self.store.has_terminal_event(task_id):
            return

        # 事件类型跟着**状态**走，不是一律 TASK_FAILED：万一哪天适配器
        # 成功返回却没发 TASK_COMPLETED，这里补的不该是一条"失败"。
        type_ = (
            EventType.TASK_COMPLETED
            if status is TaskStatus.COMPLETED
            else EventType.TASK_FAILED
        )
        payload: dict = {"reason": exit_reason or status.value, "recovered": True}
        if error:
            payload["error"] = error
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        # ``recovered: True`` 让这条能和适配器自己发的那条区分开——
        # 排查"为什么这里有一条失败事件"时，不用去猜是哪条路产生的。
        self._sink(task_id)(Event(task_id=task_id, type=type_, payload=payload))

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------

    async def cancel(self, task_id: str) -> bool:
        """中断正在跑的任务。返回 False 表示这个任务不在运行（可能已结束）。"""
        session = self._sessions.get(task_id)
        if session is None:
            return False
        await session.interrupt()
        return True

    async def wait(self, task_id: str, timeout: float | None = None) -> Task | None:
        """等任务结束（主要是给 CLI 用）。超时不会取消任务本身。"""
        runner = self._running.get(task_id)
        if runner is not None:
            if timeout is None:
                await runner
            else:
                try:
                    # shield：超时只放弃"等"，不该把还在跑的任务掐掉
                    await asyncio.wait_for(asyncio.shield(runner), timeout)
                except (TimeoutError, asyncio.TimeoutError):
                    pass
        return self.store.get_task(task_id)

    @property
    def running_count(self) -> int:
        return len(self._running)

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    def recover(self) -> list[Task]:
        """进程重启后处理上次留下的非终态任务。

        **刻意不自动重跑。** 那些任务可能有副作用——改了一半的代码、
        删了一半的文件、跑到一半的迁移。自动重跑等于在用户不知情的情况下
        再花一次钱，并且可能把副作用叠加一次。

        折中做法：标成 ``disconnected`` 并保留 ``native_session_id``，
        让手机看到"这个任务断了，要不要接着跑"，由人决定。
        续接走 ``submit(resume_from=<native_session_id>)``。

        返回被处理的任务。
        """
        orphans = self.store.active_tasks()
        if not orphans:
            return []

        now = time.time()
        sink = None
        for task in orphans:
            self.store.update_task(
                task.id,
                status=TaskStatus.DISCONNECTED,
                finished_at=now,
                exit_reason="gateway_restart",
                error="网关重启时该任务仍在运行，已标记为断开。可从原会话续接。",
            )
            # 也写一条事件：否则这段"消失的时间"在事件流里是空白的，
            # 事后回放会看到任务停在 running 就没了，像是数据丢了。
            sink = sink or self._sink(task.id)
            sink(
                Event(
                    task_id=task.id,
                    type=EventType.TASK_FAILED,
                    ts=now,
                    payload={
                        "reason": "gateway_restart",
                        "error": "网关重启，任务被标记为断开",
                    },
                )
            )
        return orphans

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """关停：取消所有在跑的任务。

        ``_execute`` 的 finally 会关掉各自的会话，所以这里只负责取消。
        """
        runners = list(self._running.values())
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)
