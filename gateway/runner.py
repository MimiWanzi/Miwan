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
import json
import time
import uuid
from pathlib import Path

from .adapters import DEFAULT_AUTONOMY, AutonomyPolicy, EmitFn, RunOutcome, get, shutdown_adapters
from .adapters.base import AgentSession
from .events import Event, EventBus, EventType
from .models import TERMINAL_STATUSES, Session, Task, TaskStatus
from .notify import Notifier, event_message
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


#: 审批卡上展示的入参上限。Write 的 content 可以是几 MB 的文件全文，
#: 手机屏幕放不下，库里也不该为一张卡片存一页纸。
_PROMPT_INPUT_LIMIT = 8000


def _cap_json(tool_input: object) -> str:
    text = json.dumps(tool_input, ensure_ascii=False, default=str)
    if len(text) > _PROMPT_INPUT_LIMIT:
        return text[:_PROMPT_INPUT_LIMIT] + " …（已截断）"
    return text


class TaskRunner:
    """提交、驱动、取消任务。"""

    def __init__(
        self,
        store: Store,
        bus: EventBus,
        default_agent: str = "claude_code",
        policy: Policy | None = None,
        prompt_base_url: str | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.default_agent = default_agent
        #: 绊线清单。只有 FULL 档用得上，但放在 runner 上而不是 api 上——
        #: 它是**配置**，一份进程一份，不该每个请求重新读一次文件
        #: （读文件本身不贵，贵的是"改了文件但没生效"这种要靠猜的 bug）。
        #: 同样判 `is None`：Policy 有 __len__，空清单是假值。
        self.policy = Policy() if policy is None else policy
        #: 权限审批通道的基地址（如 ``http://127.0.0.1:8765/mcp``）。
        #: None 表示这个进程没有 HTTP 服务（CLI 单发模式），不建审批通道。
        self.prompt_base_url = prompt_base_url
        self._running: dict[str, asyncio.Task[None]] = {}
        self._sessions: dict[str, AgentSession] = {}
        #: 每个任务的审批通道密钥：URL 上带 key，CLI 回调时必须出示。
        #: 拿不到密钥就造不出假的审批卡（能造卡不能批，但视觉骚扰也是伤害）。
        self._prompt_keys: dict[str, str] = {}
        #: 等人决策的权限请求：perm_id → future。决策到达时 set_result。
        self._pending_perms: dict[int, asyncio.Future[tuple[str, str]]] = {}
        #: 微信推送。None = 没装配（CLI 单发模式），事件路径上一个判断就绕过。
        self.notifier = notifier

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
        if agent_id is None:
            # 续接必须回到会话原本的 Agent：拿 OpenCode 的会话 id 去喂 Claude
            # 只会得到"查无此会话"。归属看两处：sessions 表（同步扫描落库的）
            # 优先；网关自己跑出来的会话来不及进那张表，从最近的任务记录里认。
            owner = None
            if resume_from:
                owner = self.store.get_session_by_native_id(resume_from)
                owner_task = self.store.latest_task_by_native_id(resume_from)
                if owner is None or not owner.agent_id:
                    owner = owner_task
            agent_id = (
                owner.agent_id if owner is not None and owner.agent_id else self.default_agent
            )
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

        # 审批通道只在"会出现权限提示"的档位开：EDIT 档除文件编辑外的每个
        # 工具都会问；FULL 档平时全预批准用不上，但它是 FULL_TOOLS 漂移的
        # 安全网——清单漏了的工具从"静默拒绝"变成"问人"。READ_ONLY 档的
        # 工具集是确定性的，永远不会有提示，通道是死重。
        prompt_url: str | None = None
        if self.prompt_base_url and autonomy in (
            AutonomyPolicy.EDIT,
            AutonomyPolicy.FULL,
        ):
            key = uuid.uuid4().hex
            self._prompt_keys[task.id] = key
            prompt_url = f"{self.prompt_base_url}/{task.id}?key={key}"

        try:
            session = adapter_cls().create_session(
                cwd=cwd,
                task_id=task.id,
                emit=sink,
                autonomy=autonomy,
                resume_from=resume_from,
                policy=self.policy,
                prompt_url=prompt_url,
            )
        except Exception as exc:  # noqa: BLE001 —— 建会话失败不能让协程带着异常消失
            self._finish(task.id, TaskStatus.FAILED, f"创建会话失败：{exc}")
            self._running.pop(task.id, None)
            self._prompt_keys.pop(task.id, None)
            return

        self._sessions[task.id] = session

        # 会话一建出来就知道 native_session_id，**立刻落库**：
        # 万一网关在这之后崩掉，手机上还能看到"断在哪个会话上"，可以续接。
        # 等到任务跑完再存是不够的——崩掉的那一刻恰恰是任务没跑完。
        # （OpenCode 例外：它的会话 id 要等第一轮 send 才分配，
        # 那部分由回合结束后的 _remember_session 补上。）
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
            self._prompt_keys.pop(task.id, None)
            self._remember_session(task, session)
            await session.close()

    def _remember_session(self, task: Task, session: AgentSession) -> None:
        """把这一轮用过的会话记进 sessions 表（项目页「历史会话」的唯一来源）。

        会话原本只从同步扫描来：任务跑完不刷新的话，手机上刚跑完任务回到
        项目页就是空的——手动添加的新项目尤其如此（OpenCode 对非 git 目录
        甚至永远扫不出来）。id 用与扫描相同的约定（``{project_id}:{native}``），
        之后扫描 upsert 到的是同一行，不会重复；续接任务再走这里时
        last_active_at 会被刷新。
        """
        native = session.native_session_id
        if not native:
            return
        self.store.upsert_session(
            Session(
                id=f"{task.project_id}:{native}",
                project_id=task.project_id,
                agent_id=task.agent_id,
                native_session_id=native,
                title=task.prompt.replace("\n", " ")[:120],
                last_active_at=time.time(),
            )
        )

    def _sink(self, task_id: str) -> EmitFn:
        """Adapter 吐事件的地方——**持久化与广播的唯一汇合点**。

        顺序很重要：**先落库拿到 seq，再广播**。反过来的话，订阅者会先收到一个
        seq=0 的事件，而它其实已经在库里了——客户端按 seq 去重时反而会把这条
        当成"还没有的"再收一遍。seq 必须在广播出门前就是最终值。
        """

        def emit(event: Event) -> None:
            self.store.append_event(event)  # 就地回填 event.seq
            self.bus.publish(event)
            self._notify(event)

        return emit

    def _notify(self, event: Event) -> None:
        """值得让手机知道的事推一条微信（哪些事值得、说什么在 notify.event_message）。

        挂在 ``emit`` 里而不是各个终点上：任务终态、权限请求、绊线拦截最终都
        从这里过，单点挂一次就不会漏。类型判断放在查库**之前**——工具事件
        一个任务几十条，别为它们多掏一次 SQLite 查询。
        """
        if self.notifier is None or event.type not in (
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
            EventType.PERMISSION_REQUEST,
        ):
            return
        task = self.store.get_task(event.task_id)
        if task is None or task.status is TaskStatus.CANCELLED:
            return
        message = event_message(task, event)
        if message is not None:
            self.notifier.notify(*message)

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
        # 任务都没了，挂着的审批请求必须立刻给出结果——否则 MCP 那头的
        # HTTP 调用要白等满整个超时，而这段时间里 CLI 进程已经死了。
        self._deny_pending(task_id, "任务已被中断")
        await session.interrupt()
        return True

    # ------------------------------------------------------------------
    # 权限审批（第 6 层的「等人批准」）
    # ------------------------------------------------------------------

    #: 手机上多久不答复就自动拒绝。30 分钟：人可能在开会、睡觉，审批不该
    #: 比一集电视剧还短；但也不能无限等——挂着不回答，CLI 进程和它的
    #: 上下文就一直占着。
    PROMPT_TIMEOUT = 1800.0

    def validate_prompt_key(self, task_id: str, key: str) -> bool:
        return bool(key) and self._prompt_keys.get(task_id) == key

    async def request_permission(
        self,
        task_id: str,
        tool_name: str,
        tool_input: object,
        timeout: float | None = None,
    ) -> dict:
        """把一个权限决策点交给手机，阻塞到有人答复（或超时自动拒绝）。

        返回值直接就是 permission-prompt-tool 约定的应答：
        ``{"behavior": "allow", "updatedInput": ...}`` 或
        ``{"behavior": "deny", "message": ...}``。

        任务状态走 WAITING_APPROVAL → RUNNING 一来一回，事件流里则是一对
        PERMISSION_REQUEST / PERMISSION_DECIDED——前端按 permission_id
        把「等你批准」的卡片换成结果。
        """
        task = self.store.get_task(task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return {"behavior": "deny", "message": "任务已结束，操作被拒绝"}

        capped = _cap_json(tool_input)
        perm_id = self.store.create_permission(
            task_id, tool_name, capped, time.time()
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, str]] = loop.create_future()
        self._pending_perms[perm_id] = future

        try:
            self.store.update_task(task_id, status=TaskStatus.WAITING_APPROVAL)
            self._sink(task_id)(
                Event(
                    task_id=task_id,
                    type=EventType.PERMISSION_REQUEST,
                    payload={
                        "permission_id": perm_id,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "source": "prompt",
                    },
                )
            )
            try:
                decision, _by = await asyncio.wait_for(
                    future, timeout or self.PROMPT_TIMEOUT
                )
                reason = "手机上拒绝了这次操作"
                auto = False
            except (TimeoutError, asyncio.TimeoutError):
                # 没人理。超时和"明确拒绝"必须走同一条落库路径，
                # 否则这条请求在库里永远悬着（decision IS NULL）。
                self.store.decide_permission(perm_id, "deny", time.time())
                decision = "deny"
                reason = f"等了 {int((timeout or self.PROMPT_TIMEOUT) / 60)} 分钟没有人答复，自动拒绝"
                auto = True
        finally:
            self._pending_perms.pop(perm_id, None)

        fresh = self.store.get_task(task_id)
        if fresh is not None and fresh.status not in TERMINAL_STATUSES:
            self.store.update_task(task_id, status=TaskStatus.RUNNING)
        self._sink(task_id)(
            Event(
                task_id=task_id,
                type=EventType.PERMISSION_DECIDED,
                payload={
                    "permission_id": perm_id,
                    "decision": decision,
                    "auto": auto,
                },
            )
        )

        if decision == "allow":
            return {"behavior": "allow", "updatedInput": tool_input}
        return {"behavior": "deny", "message": reason}

    def decide_permission(self, perm_id: int, decision: str, by: str) -> bool:
        """写入决策并唤醒等待者。HTTP 层（手机点的是/否）从这里进来。

        决策是否落库由 store 判（只进不出）；唤醒失败（future 已被超时
        拿走）不影响结果——落库了的决策就是最终结果。
        """
        if decision not in ("allow", "deny"):
            return False
        if not self.store.decide_permission(perm_id, decision, time.time()):
            return False
        future = self._pending_perms.get(perm_id)
        if future is not None and not future.done():
            future.set_result((decision, by))
        return True

    def _deny_pending(self, task_id: str, reason: str) -> None:
        """任务没了，它名下还悬着的审批请求全部就地拒绝。"""
        for perm_id in self.store.pending_permission_ids(task_id):
            if self.store.decide_permission(perm_id, "deny", time.time()):
                future = self._pending_perms.get(perm_id)
                if future is not None and not future.done():
                    future.set_result(("deny", reason))

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
        # 取消协程不会杀死挂着的审批等待（它挂在 MCP 的 HTTP 处理器上，
        # 不在 _running 里）。进程都要退了，把这些 future 逐个唤醒，
        # 不留给事件循环 "跨 cancelled 边界的悬挂 await"。
        for task_id in list(self._prompt_keys):
            self._deny_pending(task_id, "网关正在关闭")
        # Adapter 的常驻资源（OpenCode 的共享 serve 进程）在会话 close 里
        # 杀不到——它不属于任何任务，只能在这里统一收尾。
        await shutdown_adapters()
