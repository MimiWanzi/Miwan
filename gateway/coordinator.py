"""群聊协调大脑——LLM 循环 + 一组**唯一**代表"行动能力"的工具。

信任边界（这是本模块最重要的注释）：

    协调器**碰不到文件系统**。它全部的行动收敛在 :data:`_TOOL_SPECS`
    列出的几个工具上，其中真正有副作用的只有 ``dispatch_task``——它走
    ``runner.submit()``，与手机手动建的任务走完全相同的 AutonomyPolicy /
    绊线 / 审批门。大脑只能"派活"，不能"干活"。

生命周期与状态机（群状态见 :class:`gateway.models.GroupStatus`）：

    用户发言（或将来其他触发）→ wake() 排程 → 群 ACTIVE → 循环跑
    → finish / ask_user / 静默 / 轮数上限 / 出错 → 群 IDLE

  - **ask_user 结束回合而不是挂起等待**：用户可能几小时后才回，
    挂着一个协程等它毫无意义；用户再次发言会重新 wake，届时
    协调器从库里的时间线看到答案（Store 是真相，内存里不留状态）。
  - **网关重启**：循环协程随进程消失。启动时 :func:`recover_groups`
    把还挂在 ACTIVE 的群拨回 IDLE 并在时间线上说明——不自动续跑，
    同 ``runner.recover()`` 的哲学：自动重跑等于不知情的二次烧钱。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from .config import Config
from .events import Event
from .group import (
    KIND_AGENT_RESULT,
    KIND_SYSTEM,
    KIND_USER,
    GroupError,
    GroupManager,
)
from .llm import ChatResult, LLMClient, LLMError, ToolCall
from .models import TERMINAL_STATUSES, Group, GroupStatus
from .runner import TaskRunner
from .store import Store

__all__ = ["GroupCoordinator", "recover_groups"]

log = logging.getLogger("gateway.coordinator")


#: 唤醒时带进上下文的最近群消息条数。太大烧 token，太小丢上下文；
#: 群消息本来就是浓缩的（对话 + 产出摘要），40 条覆盖一次完整协作没问题。
WAKE_HISTORY_LIMIT = 40

#: 单条消息进 LLM 上下文的上限。派单 prompt 可能被写得很大，
#: 上下文爆炸是钱和注意力双输。
_MESSAGE_LIMIT = 12000

#: wait_tasks 的最长等待。与 ``runner.PROMPT_TIMEOUT`` 同口径：
#: 人在开会、在睡觉，审批和结果都不该无限等；超时如实回报现状，
#: 由模型决定是继续等还是向用户交底。
WAIT_TIMEOUT = 1800.0

#: 等任务时的轮询间隔。任务以分钟计，秒级轮询纯浪费。
_WAIT_TICK = 2.0


def recover_groups(store: Store, groups: GroupManager) -> int:
    """网关启动时把还挂在 ACTIVE 的群拨回 IDLE，并在时间线上说明。

    返回处理的群数。**只拨状态、不重跑循环**——理由见模块 docstring。
    """
    actives = [
        g for g in store.all_groups() if g.status is GroupStatus.ACTIVE
    ]
    for group in actives:
        store.update_group(group.id, status=GroupStatus.IDLE)
        try:
            groups.post_system_message(
                group.id,
                "网关重启，上一轮协调循环被打断。在群里说句话即可继续。",
            )
        except GroupError:
            pass  # 群正巧被关了：状态已拨回，说明落不进去就算了
    return len(actives)


class GroupCoordinator:
    """一次唤醒 = 一轮完整协作。无跨唤醒内存状态——上下文每次从库里重建。"""

    def __init__(
        self,
        store: Store,
        groups: GroupManager,
        runner: TaskRunner,
        client: LLMClient,
        config: Config,
        notifier=None,
    ) -> None:
        self.store = store
        self.groups = groups
        self.runner = runner
        self.client = client
        self.max_turns = config.group_max_turns
        self.autonomy = config.default_autonomy
        #: ask_user 值得推微信：协调器停下等人，手机不推用户根本不知道
        self.notifier = notifier
        self._tasks: set[asyncio.Task[None]] = set()
        #: 正在跑或已排程唤醒的群。wake 的重入保护是**内存 + 状态**双保险：
        #: 状态（ACTIVE）防跨进程，这个集合防同进程内两条并发 wake 的竞态。
        self._waking: set[str] = set()

    # ------------------------------------------------------------------
    # 唤醒
    # ------------------------------------------------------------------

    def wake(self, group_id: str) -> bool:
        """排程一次协调循环。返回 False = 没排上（群不在 / 没在 IDLE / 已排程）。

        **立即返回**，循环在后台跑——HTTP handler 不能被一次 LLM 调用
        阻塞几秒到几十秒，理由与 ``runner.submit`` 相同。
        """
        group = self.store.get_group(group_id)
        if group is None or group.status is not GroupStatus.IDLE:
            return False
        if group_id in self._waking:
            return False
        self._waking.add(group_id)
        task = asyncio.create_task(self._run(group_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(self, group_id: str) -> None:
        interrupted = False
        try:
            await self._loop(group_id)
        except asyncio.CancelledError:
            # 网关关停把循环掐了：**故意把群留在 ACTIVE**——下次启动
            # ``recover_groups`` 会拨回 IDLE 并在时间线上说明打断这回事。
            # 在这里悄悄拨回 IDLE 的话，重启后没有痕迹，用户只会觉得
            # "群里没人理我"，而问题看起来像前端的。
            interrupted = True
            raise
        except LLMError as exc:
            log.warning("群 %s 协调循环失败：%s", group_id, exc)
            self._safe_system_message(group_id, f"协调器调用失败，本轮停止：{exc}")
        except GroupError:
            pass  # 群中途被关：循环自然终止，不算故障
        except Exception:  # noqa: BLE001 —— 循环之下的任何意外都不该无声吞掉
            log.exception("群 %s 协调循环意外出错", group_id)
            self._safe_system_message(group_id, "协调循环意外出错，本轮停止。")
        finally:
            # 只有 ACTIVE 才拨回 IDLE：中途群被关成 CLOSED 时不覆盖终态；
            # 被 cancel 时根本不拨——留给 recover_groups 去交代（见上）。
            if not interrupted:
                group = self.store.get_group(group_id)
                if group is not None and group.status is GroupStatus.ACTIVE:
                    self.store.update_group(group_id, status=GroupStatus.IDLE)
            self._waking.discard(group_id)

    async def shutdown(self) -> None:
        """停掉还在跑的协调循环（网关关停时调用）。群状态故意不动，
        由下次启动的 :func:`recover_groups` 统一交代。"""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _safe_system_message(self, group_id: str, text: str) -> None:
        try:
            self.groups.post_system_message(group_id, text)
        except GroupError:
            pass

    # ------------------------------------------------------------------
    # 循环
    # ------------------------------------------------------------------

    async def _loop(self, group_id: str) -> None:
        group = self.store.get_group(group_id)
        if group is None:
            return
        self.store.update_group(group_id, status=GroupStatus.ACTIVE)

        cursor = [self.store.group_last_seq(group_id)]  # 单元素列表，_digest 就地推进
        messages = self._initial_messages(group)
        ended = False  # finish / ask_user 正常收尾

        for turn in range(1, self.max_turns + 1):
            reply = await self.client.chat(messages, tools=self._tool_specs(group))
            messages.append(self._assistant_message(reply))

            if reply.tool_calls:
                ended = await self._execute(group, reply.tool_calls, messages, cursor)
                if ended:
                    return
            else:
                if reply.content:
                    self.groups.post_coordinator_message(
                        group_id, _cap(reply.content)
                    )
                if self._pending(group_id):
                    # 模型说完话却还有没收的单：替它等，别让结果没人接
                    await self._wait_tasks(group_id, cursor)
                else:
                    return  # 静默退出：该说的说了，该收的收了

            # 注入自上一轮以来的群内新动态（用户插话、成员产出），
            # 模型下一轮才能看见。自己刚发的 dispatch/coordinator 消息
            # 不进摘要——它从工具返回值已经知道，重复只是烧 token。
            digest = self._digest(group_id, cursor)
            if digest:
                messages.append({"role": "user", "content": digest})

        if not ended:
            self._safe_system_message(
                group_id,
                f"协调循环到达轮数上限（{self.max_turns}），先停在这里。"
                "在群里说句话可以继续。",
            )

    def _initial_messages(self, group: Group) -> list[dict]:
        messages = [{"role": "system", "content": self._system_prompt(group)}]
        history = self.store.events_for_group_tail(group.id, WAKE_HISTORY_LIMIT)
        if history:
            lines = "\n".join(_format_event(ev) for ev in history)
            messages.append(
                {
                    "role": "user",
                    "content": f"[群里的最近动态，最旧的在前]\n{lines}",
                }
            )
        return messages

    def _system_prompt(self, group: Group) -> str:
        members = "\n".join(
            f"- {m}（{self.groups._display(m)}）" for m in group.members
        )
        project = self.store.get_project(group.project_id)
        cwd = project.path if project else "（未知）"
        return (
            "你是「队长」——Miwan 群聊里的协调者。"
            "群成员是这台电脑上真实安装的 CLI 编码 Agent，"
            "它们能读写文件、执行命令、跑构建与测试。"
            "你协调它们完成用户的目标；你自己不直接改文件或执行命令，"
            "唯一的行动方式是调用工具。\n\n"
            f"本群成员：\n{members}\n\n"
            f"项目工作目录：{cwd}\n\n"
            "行为准则：\n"
            "- 先判断要不要拆活、派给谁。一个成员能干的别派两个——"
            "派单烧的是真实额度。\n"
            "- **项目级的请求**（多个模块、多步走、需要定方案）先别派单：用 post_message "
            "发一份需求文档到群里（目标、模块划分、各成员的领地、验收标准），"
            "然后 ask_user 请用户审阅。用户要改就改完再问；用户明确通过后才拆解派单。"
            "文档用【需求文档】开头并带版本号，方便用户对照。"
            "一句话能干的小活不要走这套流程，直接派。\n"
            "- 拆解时给每个成员**划清领地**：每张单子里写明只动哪些目录/文件、"
            "不要碰什么；两个成员的领地不得重叠；共享文件（配置、入口、依赖清单）"
            "的修改单独串行安排，不和领地内的活并行；有依赖的活串行"
            "（先地基后上层），无依赖的才并行。\n"
            "- 多个成员的活**相互没有依赖时，在同一轮里把 dispatch_task 一起发出去**"
            "（一次回复带多个工具调用），让它们同时开工；全部派完再 wait_tasks。"
            "不要派一个等一个——并行是团队的意义。\n"
            "- dispatch_task 的 prompt 必须自包含：成员看不到群聊历史，"
            "把背景、目标、验收标准写清楚。\n"
            "- 派完单用 wait_tasks 等结果；拿到的是成员自报的产出，"
            "向用户转述时保持忠实，不夸大不缩水。\n"
            "- 成员的产出会自动出现在群里，所有人都看得到——你只提炼要点做推进，"
            "不要在群里原样复述成员已经说过的话。\n"
            "- 需要用户拍板就用 ask_user 结束回合，不要自作主张。\n"
            "- 全部收尾时用 finish 给用户一个简短总结。\n"
            "- 用户只是问问题、无需派单时，直接回答（发完这条你的回合就结束了）。\n"
            "- 用中文说话。"
        )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _tool_specs(self, group: Group) -> list[dict]:
        """OpenAI tools 格式的工具清单。成员枚举按群现算——
        协调器不该能派单给不在群里的人。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "post_message",
                    "description": "在群里发言。用于说明计划、回答问题、汇报进展。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "要说的话"}
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "dispatch_task",
                    "description": (
                        "派一个任务给群成员（真实 CLI Agent，会真的干活并消耗额度）。"
                        "prompt 必须自包含：成员看不到群聊历史。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agent_id": {
                                "type": "string",
                                "enum": list(group.members),
                                "description": "接活的成员",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "任务说明（背景、目标、验收标准）",
                            },
                        },
                        "required": ["agent_id", "prompt"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "wait_tasks",
                    "description": (
                        "阻塞等待本群所有派出任务到终态，返回各自的状态与产出摘要。"
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ask_user",
                    "description": (
                        "向用户提问并结束本轮——用户回复后你会被再次唤醒。"
                        "用于需要人拍板的事。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "问题"}
                        },
                        "required": ["question"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finish",
                    "description": "本轮协作收尾：给用户一个简短总结，然后群回到待命。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "description": "总结"}
                        },
                        "required": ["summary"],
                    },
                },
            },
        ]

    async def _execute(
        self, group: Group, calls: list[ToolCall], messages: list[dict],
        cursor: list[int] | None = None,
    ) -> bool:
        """逐个执行工具调用并把结果作为 tool 消息回填。返回 True = 本轮该结束
        （ask_user / finish 被调用）。**后续调用仍然执行完**——
        已经产生的调用不回滚，只是本轮不再继续。

        ``cursor`` 是 digest 游标（单元素列表，就地推进），透传给
        wait_tasks 做插话检测；None 表示调用方不关心（探针直连工具时）。
        """
        ended = False
        for call in calls:
            try:
                args = json.loads(call.arguments) if call.arguments else {}
            except ValueError:
                args = None
            if not isinstance(args, dict):
                result = {"error": "工具参数不是合法的 JSON 对象"}
            else:
                try:
                    result = await self._dispatch_tool(group, call.name, args, cursor)
                    if call.name in ("ask_user", "finish"):
                        ended = True
                except GroupError as exc:
                    result = {"error": str(exc)}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        return ended

    async def _dispatch_tool(
        self, group: Group, name: str, args: dict, cursor: list[int] | None = None
    ) -> dict:
        if name == "post_message":
            text = str(args.get("text") or "").strip()
            if not text:
                return {"error": "text 不能为空"}
            self.groups.post_coordinator_message(group.id, _cap(text))
            return {"ok": True}

        if name == "dispatch_task":
            agent_id = str(args.get("agent_id") or "").strip()
            prompt = str(args.get("prompt") or "").strip()
            if agent_id not in group.members:
                return {
                    "error": f"{agent_id or '（空）'} 不在本群成员里：{group.members}"
                }
            if not prompt:
                return {"error": "prompt 不能为空"}
            task = self.runner.submit(
                project_id=group.project_id,
                prompt=prompt,
                agent_id=agent_id,
                autonomy=self.autonomy,
                group_id=group.id,
            )
            self.groups.post_dispatch(group.id, task.id, agent_id, _cap(prompt, 2000))
            return {
                "task_id": task.id,
                "agent_id": agent_id,
                "note": "任务已提交。用 wait_tasks 等它结束。",
            }

        if name == "wait_tasks":
            return {"tasks": await self._wait_tasks(group.id, cursor)}

        if name == "ask_user":
            question = str(args.get("question") or "").strip()
            if not question:
                return {"error": "question 不能为空"}
            self.groups.post_coordinator_message(group.id, _cap(question))
            self._notify_ask(group, question)
            return {"ok": True, "note": "已向用户提问，回合结束；用户回复后你会被唤醒。"}

        if name == "finish":
            summary = str(args.get("summary") or "").strip()
            if not summary:
                return {"error": "summary 不能为空"}
            self.groups.post_coordinator_message(group.id, _cap(summary))
            return {"ok": True, "note": "本轮已收尾。"}

        return {"error": f"未知工具：{name}"}

    def _notify_ask(self, group: Group, question: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.notify(
                f"群聊等你回复：{group.title}", _cap(question, 200)
            )
        except Exception:  # noqa: BLE001 —— 推送失败不该打断协调循环
            log.exception("ask_user 推送失败（群 %s）", group.id)

    # ------------------------------------------------------------------
    # 任务等待与上下文
    # ------------------------------------------------------------------

    async def _wait_tasks(
        self, group_id: str, cursor: list[int] | None = None
    ) -> list[dict]:
        """等到本群所有任务进终态（或超时），返回全部任务的现状。

        超时**不是失败**：把 running 的如实报给模型，它可以选择继续等
        （再调一次）或向用户交底。用户在等待期间**插话也提前收摊**——
        群是用户的，队长的"等"不该变成对用户装聋；打断后任务照常在跑，
        插话由下一轮 digest 递给模型，它自己决定先答话还是接着等。
        """
        deadline = time.monotonic() + WAIT_TIMEOUT
        while True:
            tasks = self.store.tasks_for_group(group_id)
            if not any(t.status not in TERMINAL_STATUSES for t in tasks):
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_WAIT_TICK)
            if cursor is not None:
                fresh = self.store.events_for_group(group_id, cursor[0], limit=50)
                if any(e.payload.get("kind") == KIND_USER for e in fresh):
                    break

        out = []
        for t in self.store.tasks_for_group(group_id):
            if t.status.value == "completed":
                summary = self.store.last_assistant_text(t.id)
            else:
                summary = t.error or self.store.last_assistant_text(t.id)
            out.append(
                {
                    "task_id": t.id,
                    "agent_id": t.agent_id,
                    "status": t.status.value,
                    "summary": _cap(summary or "（无输出）", 2000),
                }
            )
        return out

    def _pending(self, group_id: str) -> bool:
        tasks = self.store.tasks_for_group(group_id)
        return any(t.status not in TERMINAL_STATUSES for t in tasks)

    def _digest(self, group_id: str, cursor_ref: list[int]) -> str | None:
        """cursor_ref 是单元素列表（[seq]），就地推进——比返回 tuple 的调用点干净。"""
        cursor = cursor_ref[0]
        events = self.store.events_for_group(group_id, cursor)
        if not events:
            return None
        cursor_ref[0] = events[-1].seq
        lines = [
            _format_event(ev)
            for ev in events
            if ev.payload.get("kind") in (KIND_USER, KIND_SYSTEM, KIND_AGENT_RESULT)
        ]
        if not lines:
            return None
        return "[群内新动态]\n" + "\n".join(lines)

    @staticmethod
    def _assistant_message(reply: ChatResult) -> dict:
        """把一轮回复还原成 OpenAI 协议要求的 assistant 消息形状——
        tool_calls 必须原样带回去，否则下一次请求会被端点拒绝。"""
        message: dict = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in reply.tool_calls
            ]
        return message


def _format_event(event: Event) -> str:
    """一条群消息 → 给 LLM 看的一行。与前端渲染共用 payload，但这里是
    纯文本压缩版：卡片类只留要害。"""
    p = event.payload or {}
    kind = p.get("kind")
    clock = time.strftime("%H:%M", time.localtime(event.ts))
    if kind == KIND_USER:
        return f"[{clock}] 用户说：{p.get('text', '')}"
    if kind == "coordinator":
        return f"[{clock}] 你（队长）说：{p.get('text', '')}"
    if kind == KIND_SYSTEM:
        return f"[{clock}] 系统：{p.get('text', '')}"
    if kind == "dispatch":
        return (
            f"[{clock}] 你派单 {p.get('task_id')} → {p.get('agent_id')}："
            f"{_cap(str(p.get('prompt', '')), 200)}"
        )
    if kind == KIND_AGENT_RESULT:
        status = p.get("status", "?")
        return (
            f"[{clock}] 成员 {p.get('agent_id')}（任务 {p.get('task_id')}，{status}）"
            f"产出：{_cap(str(p.get('text') or '（无输出）'), 2000)}"
        )
    return f"[{clock}] {json.dumps(p, ensure_ascii=False)[:200]}"


def _cap(text: str, limit: int = _MESSAGE_LIMIT) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + " …（已截断）"
