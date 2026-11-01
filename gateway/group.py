"""群聊（多智能体协同）的持久化与事件出口。

群是**编排层的概念**，不是某个 Agent 的私货：它把多个 Agent 的任务与一条
聊天时间线装在同一个视图里。本模块只依赖 store / bus / adapters 注册表，
与 :mod:`gateway.runner` 的分工是——

  - **runner** 管单个任务的生命周期（怎么跑、事件怎么落）
  - **group** 管群的形态（谁在群里、时间线上有什么、群是什么状态）

时间线也是事件流：群消息以 ``EventType.GROUP_MESSAGE`` 落库，带 ``group_id``，
seq 语义与任务事件完全一致——轮询 ``?since=N`` 与实时推送同源，
断线补发免费获得（CLAUDE.md 规则 5/6 对群自动成立）。

消息的 payload 按用途分 kind（``KIND_*`` 常量）：文本类（user / coordinator /
system）直接渲染；卡片类（dispatch / agent_result）带 task_id / agent_id，
前端渲染成可点进任务页的卡片。协调循环（第 2 层）落消息也走这里的 ``_emit``，
所以"群里有什么"只有这一个写入口。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

from .adapters import get as get_adapter
from .events import Event, EventBus, EventType
from .models import Group, GroupStatus
from .runner import RunnerError, UnknownAgent, UnknownProject
from .store import Store

__all__ = [
    "GroupError",
    "GroupManager",
    "GroupTaskWatcher",
    "KIND_AGENT_RESULT",
    "KIND_COORDINATOR",
    "KIND_DISPATCH",
    "KIND_SYSTEM",
    "KIND_USER",
    "resolve_mentions",
]

log = logging.getLogger("gateway.group")


# ----------------------------------------------------------------------
# 群消息的 kind 词表。前端按它选渲染形态；协调循环也只用这些。
# ----------------------------------------------------------------------

KIND_USER = "user"
KIND_COORDINATOR = "coordinator"
KIND_SYSTEM = "system"
KIND_DISPATCH = "dispatch"
KIND_AGENT_RESULT = "agent_result"

# ----------------------------------------------------------------------
# @路由：用户在消息里点名成员，被点到的直接开工，不用等队长转手。
#
# 这是 MACH（Kimi 多智能体协作）验证过的交互：
#   @某成员   → 只有他处理（点名即派单，最快路径）
#   @全体成员 → 全员并行处理
#   无 @      → 队长（协调器）照常安排
# ----------------------------------------------------------------------

_AT_ALL = {"全体成员", "全体", "all", "所有人", "everyone"}
# @token 只截到空白/常用标点为止；@Claude Code 点的是 @Claude
_AT_PATTERN = re.compile(r"(?:^|\s)@([^\s@,，。！？、；：]+)")


def _norm(name: str) -> str:
    """匹配用的归一化：小写、去空白/_/-。@ClaudeCode 与 claude_code 相遇在这里。"""
    return re.sub(r"[\s_\-]+", "", name.lower())


def resolve_mentions(text: str, members: list[str]) -> list[str]:
    """解析消息里的 @，返回被点名的成员（保序、去重、都是 members 的子集）。

    匹配键是 agent_id 与 display_name 的归一化形式。**精确命中优先**：
    一个 token 有人精确对上就只算他，不再做前缀扩展——群里同时有
    fakebot 和 fakebot2 时，@fakebot 不该把 fakebot2 也捎上。
    没有精确命中时，长度 ≥3 的 token 允许作为前缀命中（@claude →
    claude_code，@codex 打到一半的 @code 也算数）。
    没点中任何人返回空表——调用方据此走队长的老路，两态互不干扰。
    """
    tokens = [m.group(1) for m in _AT_PATTERN.finditer(text)]
    if not tokens:
        return []
    if any(t.lower() in _AT_ALL for t in tokens):
        return list(members)

    def keys_of(member: str) -> tuple[str, str]:
        adapter_cls = get_adapter(member)
        display = adapter_cls.display_name if adapter_cls else member
        return (_norm(member), _norm(display))

    picked: list[str] = []
    for token in tokens:
        tn = _norm(token)
        if len(tn) < 2:
            continue  # 单字符 @ 是手滑，不是点名
        exact = [m for m in members if m not in picked and tn in keys_of(m)]
        if exact:
            picked.extend(exact)
            continue
        if len(tn) >= 3:
            picked.extend(
                m for m in members
                if m not in picked and any(k.startswith(tn) for k in keys_of(m))
            )
    return picked


class GroupError(RuntimeError):
    """建群 / 发言的前置条件不满足。"""


class UnknownAgent(GroupError):
    pass


class GroupManager:
    """建群、往时间线落消息。**只做持久化与广播**，协调逻辑在第 2 层。"""

    def __init__(self, store: Store, bus: EventBus) -> None:
        self.store = store
        self.bus = bus

    # ------------------------------------------------------------------
    # 建群
    # ------------------------------------------------------------------

    def create_group(
        self, *, project_id: str, title: str, members: list[str]
    ) -> Group:
        """校验项目与成员，落库，并在时间线上留一条建立记录。

        成员在这里就校验，而不是等派单时才炸：建群的人得**当场**知道
        "Codex 没注册"这种错，不然群里安静地少一个成员，谁都说不清为什么。
        """
        project = self.store.get_project(project_id)
        if project is None:
            raise UnknownProject(f"库里没有这个项目：{project_id}")

        # 去重保序：同一 agent 勾两次不该在群里出现两个。
        # 空清单挡掉——没有成员的群永远说不上话，建它只是在制造误会。
        seen: list[str] = []
        for raw in members:
            agent_id = str(raw).strip()
            if not agent_id or agent_id in seen:
                continue
            if get_adapter(agent_id) is None:
                raise UnknownAgent(f"未注册的 Agent：{agent_id}")
            seen.append(agent_id)
        if not seen:
            raise GroupError("至少要选一个群成员")

        group = Group(
            id=f"g-{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            title=title.strip() or f"群聊 {time.strftime('%m-%d %H:%M')}",
            members=seen,
            status=GroupStatus.IDLE,
            created_at=time.time(),
        )
        self.store.create_group(group)
        self._emit(
            group.id,
            {
                "kind": KIND_SYSTEM,
                "text": "群已建立。成员：" + "、".join(self._display(m) for m in seen),
            },
        )
        return group

    def _display(self, agent_id: str) -> str:
        adapter_cls = get_adapter(agent_id)
        return adapter_cls.display_name if adapter_cls else agent_id

    # ------------------------------------------------------------------
    # 发言
    # ------------------------------------------------------------------

    def post_user_message(self, group_id: str, text: str) -> Event:
        """用户的一条发言。返回落了库（已带 seq）的事件。"""
        return self._emit_checked(
            group_id, {"kind": KIND_USER, "text": text.strip()}
        )

    def post_system_message(self, group_id: str, text: str) -> Event:
        """系统插话：网关重启打断协调循环一类的事，用系统口吻留在时间线上。"""
        return self._emit_checked(group_id, {"kind": KIND_SYSTEM, "text": text})

    def post_coordinator_message(self, group_id: str, text: str) -> Event:
        """协调器（队长）的发言。文本入口只有一个，超长由调用方裁。"""
        return self._emit_checked(
            group_id, {"kind": KIND_COORDINATOR, "text": text}
        )

    def post_dispatch(
        self, group_id: str, task_id: str, agent_id: str, prompt: str,
        by: str = "coordinator",
    ) -> Event:
        """一张派单卡：告诉群里"派了什么活给谁"。任务本身照常在任务页看。

        ``by`` 记录派单的发起方（"coordinator" 队长编排 / "user" 用户 @ 点名），
        前端据此区分「队长 派单给…」和「你 叫醒了…」——同一张卡，两种口吻。
        """
        return self._emit_checked(
            group_id,
            {
                "kind": KIND_DISPATCH,
                "task_id": task_id,
                "agent_id": agent_id,
                "prompt": prompt,
                "by": by,
            },
        )

    def post_agent_result(
        self, group_id: str, task_id: str, agent_id: str, status: str, text: str | None
    ) -> Event:
        """成员任务的产出回灌。text 缺了就如实空着——不编造"（无输出）"以外的内容。"""
        return self._emit_checked(
            group_id,
            {
                "kind": KIND_AGENT_RESULT,
                "task_id": task_id,
                "agent_id": agent_id,
                "status": status,
                "text": text,
            },
        )

    # ------------------------------------------------------------------
    # 事件出口——群里唯一的写入口
    # ------------------------------------------------------------------

    def _emit_checked(self, group_id: str, payload: dict) -> Event:
        """发言前先确认群存在且没被关掉。

        建群与发言都过这道检查，**而不是只信 HTTP 层**：第 2 层的协调循环
        异步落消息时同样走 :meth:`_emit_checked`——循环跑着跑着群被人关了，
        消息必须被挡下，而不是落进一个已关闭的群里。
        """
        group = self.store.get_group(group_id)
        if group is None:
            raise GroupError(f"没有这个群：{group_id}")
        if group.status is GroupStatus.CLOSED:
            raise GroupError("群已关闭，不能再发言")
        return self._emit(group_id, payload)

    def _emit(self, group_id: str, payload: dict) -> Event:
        """落库拿 seq、再广播——顺序与 ``runner._sink`` 相同，理由也相同：
        广播出门前 seq 必须已经是最终值，否则按 seq 去重的客户端会收重。"""
        event = Event(
            task_id="",
            type=EventType.GROUP_MESSAGE,
            payload=payload,
            group_id=group_id,
        )
        self.store.append_event(event)  # 就地回填 event.seq
        self.bus.publish(event)
        return event


#: 回灌到群时间线的产出正文上限。成员的一条结论可能带几页代码，
#: 群是给人看动态的，不是任务页的替身——超了就截断，全量去任务页看。
_RESULT_TEXT_LIMIT = 6000


class GroupTaskWatcher:
    """盯总线：群里派出的任务到了终态，就把产出灌回群时间线。

    为什么不塞进协调循环：循环会被网关重启打断、会被轮数上限停掉，
    而任务在那些时刻**照样会完成**。回灌挂在总线这个"有事发生了"的
    信号上，只要进程活着就会发生——Store 是真相（CLAUDE.md 规则 4），
    协调器在不在场不影响的实事，就不该依赖协调器。
    """

    def __init__(self, store: Store, bus: EventBus, groups: GroupManager) -> None:
        self.store = store
        self.bus = bus
        self.groups = groups
        self._queue: asyncio.Queue[Event] | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._queue = self.bus.subscribe()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        if self._queue is not None:
            self.bus.unsubscribe(self._queue)
            self._queue = None

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                self._handle(event)
            except GroupError:
                pass  # 群没了/关了——回灌被挡下是正确行为，不是故障
            except Exception:  # noqa: BLE001 —— 观察者不能被单条事件弄死
                log.exception("群任务回灌处理失败（task=%s）", event.task_id)

    def _handle(self, event: Event) -> None:
        if event.type not in (EventType.TASK_COMPLETED, EventType.TASK_FAILED):
            return
        task = self.store.get_task(event.task_id)
        if task is None or not task.group_id:
            return
        if task.status.value == "completed":
            text = self.store.last_assistant_text(task.id)
        else:
            # 失败/取消：错误原因比最后一条发言更有信息量；都没有就空着
            text = task.error or self.store.last_assistant_text(task.id)
        if text and len(text) > _RESULT_TEXT_LIMIT:
            text = text[:_RESULT_TEXT_LIMIT] + " …（已截断，全量在任务页）"
        self.groups.post_agent_result(
            task.group_id, task.id, task.agent_id, task.status.value, text
        )
