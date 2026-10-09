"""驱动 Codex CLI 的 ``codex exec`` 进程。

进程模型与 Claude/WorkBuddy 都不同：**每回合一个进程**。``codex exec``
跑完即退，没有常驻 stdin 可写，所以 ``send()`` = 起进程 → prompt 从
stdin 灌入（``-`` 参数）→ 读 JSONL 到 EOF → 收终态。回合间状态只有
thread_id 一项，续接靠 ``codex exec resume <thread_id>``。

thread_id 的获取（0.162.0-alpha.2 实测）：首条事件 ``thread.started``
带顶层 ``thread_id``；resume 续轮会**重新发出同一个 id**。构造时
native_session_id 为 None，首轮 send 中学到——runner 对"首轮 send 之后
才有了会话 id"有现成处理（``runner.py`` 的 ``_remember_session``，
OpenCode 先例），核心零改动。

权限模型：非交互 exec **没有权限提示环节**，自主度由启动期
``-s`` sandbox 一次性决定（映射见 :data:`SANDBOX_BY_AUTONOMY`）。
FULL 档的绊线（Policy）在 exec 模式下**没有拦截点可注入**——这是
capability ``permission_gate=False`` 的实义，不是偷懒。红线照旧：
``--dangerously-bypass-approvals-and-sandbox`` 那个把沙箱和审批一起关掉的
旗标永远不用；``-s danger-full-access`` 是正式的沙箱档位（-rollout 里
留有 sandbox_policy 记录），且经用户拍板。

resume 的限制（实测硬报错）：``exec resume`` 是独立子命令，**不接受
``-C`` 也不接受 ``-s``**——工作目录与沙箱策略继承首轮。所以首轮独有
的旗标只在首轮传，resume 轮一个都不传。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path

from ...events import EventType
from ...permissions import Policy
from ..anthropic_stream import flatten_content
from ..base import AgentSession, AutonomyPolicy, EmitFn, RunOutcome
from .locate import clean_env

#: AutonomyPolicy → codex 的 sandbox 档（``-s``）。映射有损：codex 没有
#: "运行时问人"的档位，三档只是沙箱边界不同。
SANDBOX_BY_AUTONOMY = {
    AutonomyPolicy.READ_ONLY: "read-only",
    AutonomyPolicy.EDIT: "workspace-write",
    AutonomyPolicy.FULL: "danger-full-access",
}

#: 工具名约定：rollout 文件里 shell 命令走 ``shell``、文件编辑走
#: ``apply_patch``，事件流里沿用这两个名字，与历史页（discovery 还原的
#: function_call/custom_tool_call）对得上。
SHELL_TOOL_NAME = "shell"
PATCH_TOOL_NAME = "apply_patch"


class _Stream:
    """把一回合的 codex exec JSONL 归一化成网关事件。

    有状态、**每回合新建**（错误去重、终态判定都是回合内状态）；
    thread_id 通过 ``native_session_id`` 从会话带来、只学不改——
    resume 续轮重发的同一 id 不覆盖显式指定的续接目标。

    item 只取 ``completed``：``started``/``in_progress`` 是同一 item 的
    中间态，都发会重复（实测 command_execution 成对出现，前者
    exit_code=null）。``reasoning``/``todo_list`` 是纯噪音，丢弃（同
    Claude 的 thinking_tokens 惯例）。``error`` item **不是失败**——
    0.162 每轮固定发两条废弃配置项警告，turn 照常完成；按连续重复去重
    后降级 RAW（唯一失败信号是 ``turn.failed``）。
    """

    def __init__(self, *, prompt: str, cwd: str, native_session_id: str | None) -> None:
        self.native_session_id = native_session_id
        self.terminal: str | None = None
        """回合终态："completed" / "failed"，流结束时仍是 None 说明没等到。"""
        self.failure_error: str | None = None
        self.usage: dict | None = None
        self._prompt = prompt
        self._cwd = cwd
        self._last_error: str | None = None

    def handle(self, record: dict) -> list[tuple[EventType, dict]]:
        rtype = record.get("type")

        if rtype == "thread.started":
            thread_id = record.get("thread_id")
            if isinstance(thread_id, str) and thread_id and self.native_session_id is None:
                self.native_session_id = thread_id
            return []

        if rtype == "turn.started":
            # TASK_STARTED 形状对齐 Claude 的 system/init（模型/工具集
            # exec 模式拿不到，如实给 None）
            return [
                (
                    EventType.TASK_STARTED,
                    {
                        "prompt": self._prompt,
                        "cwd": self._cwd,
                        "model": None,
                        "tools": None,
                        "native_session_id": self.native_session_id,
                    },
                )
            ]

        if rtype == "item.completed":
            item = record.get("item")
            if isinstance(item, dict):
                return self._item(item)
            return []

        if rtype == "turn.completed":
            self.terminal = "completed"
            usage = record.get("usage")
            self.usage = usage if isinstance(usage, dict) else None
            return []

        if rtype == "turn.failed":
            self.terminal = "failed"
            error = record.get("error")
            if isinstance(error, dict):
                self.failure_error = str(error.get("message") or error)
            elif error is not None:
                self.failure_error = str(error)
            return []

        if rtype in ("item.started",):
            return []  # 中间态，见类 docstring

        # 未识别类型降级 RAW 不丢——新版 CLI 加事件类型时至少看得见
        return [
            (
                EventType.RAW,
                {
                    "reason": "未知记录类型",
                    "line": json.dumps(record, ensure_ascii=False)[:2000],
                },
            )
        ]

    def _item(self, item: dict) -> list[tuple[EventType, dict]]:
        itype = item.get("type")
        item_id = item.get("id")

        if itype == "agent_message":
            text = str(item.get("text") or "")
            if not text:
                return []
            return [(EventType.ASSISTANT_TEXT, {"text": text, "parent_tool_use_id": None})]

        if itype == "command_execution":
            command = str(item.get("command") or "")
            exit_code = item.get("exit_code")
            return [
                (
                    EventType.TOOL_CALL,
                    {
                        "id": item_id,
                        "name": SHELL_TOOL_NAME,
                        "input": {"command": command},
                        "parent_tool_use_id": None,
                    },
                ),
                (
                    EventType.TOOL_RESULT,
                    {
                        "tool_use_id": item_id,
                        # flatten_content 顺带做 4000 字符截断（与 Claude 同一-limit）
                        "content": flatten_content(item.get("aggregated_output")),
                        # None（未知）不算失败：成败以 turn 终态为准
                        "is_error": exit_code not in (0, None),
                        "parent_tool_use_id": None,
                    },
                ),
            ]

        if itype == "file_change":
            changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
            summary = "；".join(f"{c.get('kind')}: {c.get('path')}" for c in changes)
            status = item.get("status")
            return [
                (
                    EventType.TOOL_CALL,
                    {
                        "id": item_id,
                        "name": PATCH_TOOL_NAME,
                        "input": {"changes": changes},
                        "parent_tool_use_id": None,
                    },
                ),
                (
                    EventType.TOOL_RESULT,
                    {
                        "tool_use_id": item_id,
                        "content": summary or str(status or ""),
                        "is_error": status not in (None, "completed"),
                        "parent_tool_use_id": None,
                    },
                ),
            ]

        if itype in ("mcp_tool_call", "web_search"):
            # 形状未实测捕获（本轮协议探针没触发）——只发 TOOL_CALL，
            # 不编造 TOOL_RESULT；其余字段原样透传
            rest = {k: v for k, v in item.items() if k not in ("id", "type", "status")}
            return [
                (
                    EventType.TOOL_CALL,
                    {
                        "id": item_id,
                        "name": str(item.get("tool") or itype),
                        "input": rest,
                        "parent_tool_use_id": None,
                    },
                )
            ]

        if itype == "error":
            message = str(item.get("message") or "")
            # 连续重复去重（0.162 的废弃配置警告每轮原样发两条）
            if not message or message == self._last_error:
                return []
            self._last_error = message
            return [(EventType.RAW, {"reason": "codex_item_error", "message": message[:2000]})]

        if itype in ("reasoning", "todo_list"):
            return []  # 纯噪音

        return [
            (
                EventType.RAW,
                {
                    "reason": "未知 item 类型",
                    "type": str(itype),
                    "line": json.dumps(item, ensure_ascii=False)[:2000],
                },
            )
        ]


class CodexSession(AgentSession):
    """一个 Codex 会话句柄。进程在每次 ``send()`` 时拉起、结束时退出。"""

    def __init__(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        task_id: str,
        emit: EmitFn,
        autonomy: AutonomyPolicy = AutonomyPolicy.READ_ONLY,
        resume_from: str | None = None,
        policy: Policy | None = None,
        prompt_url: str | None = None,
    ) -> None:
        super().__init__(
            cwd=cwd,
            task_id=task_id,
            emit=emit,
            autonomy=autonomy,
            resume_from=resume_from,
        )
        self._command = command
        self._resume_from = resume_from
        self._first_turn = True
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._interrupted = False
        # policy 收下但不注入：exec 模式没有运行时拦截点（见模块 docstring），
        # 绊线内容保留在任务记录里可审计，暂无法落到 codex 侧。
        self.policy = Policy() if policy is None else policy
        # prompt_url 收下但不用：非交互 exec 没有权限提示通道（同 WorkBuddy）。
        self._prompt_url = prompt_url

    # ------------------------------------------------------------------
    # AgentSession 接口
    # ------------------------------------------------------------------

    async def send(self, prompt: str) -> RunOutcome:
        if self._closed:
            raise RuntimeError("会话已关闭")

        # resume 判定：显式 resume_from、或已经不是首轮且 thread_id 已知。
        # 首轮没拿到 thread_id 的退化场合只能另起新线程继续（上下文已丢）。
        resume = self.native_session_id is not None and (
            self._resume_from is not None or not self._first_turn
        )
        self._first_turn = False

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command,
                *self._build_args(resume),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd),
                env=clean_env(),
            )
        except OSError as exc:
            error = f"启动 codex 进程失败：{exc}"
            self._emit(EventType.TASK_FAILED, {"reason": "spawn_failed", "error": error})
            return RunOutcome(
                ok=False,
                reason="spawn_failed",
                error=error,
                native_session_id=self.native_session_id,
            )

        self._proc = proc
        self._stderr_tail.clear()
        self._interrupted = False
        stderr_task = asyncio.create_task(self._drain_stderr(proc))

        stream = _Stream(prompt=prompt, cwd=str(self.cwd), native_session_id=self.native_session_id)
        stdin_error: str | None = None
        try:
            try:
                assert proc.stdin is not None
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()  # `-` 模式读到 EOF 才开始跑
            except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError) as exc:
                stdin_error = f"写入 stdin 失败，进程可能已退出：{exc}"
            else:
                await self._pump(proc, stream)
                try:
                    await proc.wait()
                except (RuntimeError, ProcessLookupError):
                    pass
        finally:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            self._proc = None

        duration_ms = int((time.monotonic() - started) * 1000)
        return self._finish(stream, duration_ms, stdin_error)

    async def interrupt(self) -> None:
        """中断当前轮——codex 会拉起 PowerShell 子进程，要连树一起砍。"""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._interrupted = True
        await self._kill_tree(proc)
        try:
            await proc.wait()
        except (RuntimeError, ProcessLookupError):
            pass

    async def close(self) -> None:
        """释放进程。可重复调用；每回合模型下多数调用发生在回合之间（无事可做）。"""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            await self._kill_tree(proc)
            try:
                await proc.wait()
            except asyncio.CancelledError:
                pass
        self._proc = None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_args(self, resume: bool) -> list[str]:
        """argv。``-`` = prompt 从 stdin 读。

        首轮带 ``-C``（工作目录）与 ``-s``（沙箱档）；resume 轮**都不带**——
        实测 resume 子命令对它们硬报错，两者都继承首轮。
        """
        if resume:
            return [
                "exec", "resume", self.native_session_id or "",
                "--json", "--skip-git-repo-check", "-",
            ]
        sandbox = SANDBOX_BY_AUTONOMY[self.autonomy]
        return [
            "exec", "--json", "--skip-git-repo-check",
            "-C", str(self.cwd), "-s", sandbox, "-",
        ]

    async def _pump(self, proc: asyncio.subprocess.Process, stream: _Stream) -> None:
        """读 stdout 到 EOF，逐条归一化并发出。刻意没有单轮超时（同 Claude）。"""
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            # stdin 模式的固定前言行（实测），不是事件
            if line.startswith("Reading additional input from stdin"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                self._emit(EventType.RAW, {"reason": "非 JSON 输出行", "line": line[:2000]})
                continue
            if not isinstance(record, dict):
                continue
            for type_, payload in stream.handle(record):
                self._emit(type_, payload)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """持续排空 stderr，否则子进程可能阻塞在写 stderr 上（同 Claude）。"""
        if proc.stderr is None:
            return
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._stderr_tail.append(line)

    def _finish(self, stream: _Stream, duration_ms: int, stdin_error: str | None) -> RunOutcome:
        """终态判定。优先级：stdin 写失败 > turn 终态 > 主动中断 > 流提前结束。

        中断放在终态之后：kill 落地前 turn.completed 已刷出来的话，以事实
        为准报完成。
        """
        native = stream.native_session_id
        self.native_session_id = native

        if stdin_error is not None:
            self._emit(EventType.TASK_FAILED, {"reason": "stdin_closed", "error": stdin_error})
            return RunOutcome(ok=False, reason="stdin_closed", error=stdin_error, native_session_id=native)

        if stream.terminal == "completed":
            # usage 是 token 计数（0.162 不给 USD）：cost_usd 如实 None，
            # 用量原样带给上层（cost_reporting=True 的实义）
            payload = {
                "reason": "completed",
                "is_error": False,
                "cost_usd": None,
                "duration_ms": duration_ms,
                "num_turns": 1,  # exec 一轮就是一次 send
                "usage": stream.usage,
            }
            self._emit(EventType.TASK_COMPLETED, payload)
            return RunOutcome(ok=True, reason="completed", cost_usd=None, duration_ms=duration_ms, native_session_id=native)

        if stream.terminal == "failed":
            error = stream.failure_error or "codex turn.failed（无错误详情）"
            self._emit(EventType.TASK_FAILED, {"reason": "turn_failed", "is_error": True, "error": error})
            return RunOutcome(ok=False, reason="turn_failed", error=error, native_session_id=native)

        if self._interrupted:
            error = "本轮被主动中断"
            self._emit(EventType.TASK_FAILED, {"reason": "cancelled", "is_error": True, "error": error})
            return RunOutcome(ok=False, reason="cancelled", error=error, native_session_id=native)

        tail = "；".join(self._stderr_tail) or "（stderr 无输出）"
        error = f"事件流在收到 turn 终态之前就结束了，进程可能已崩溃。stderr: {tail}"
        self._emit(EventType.TASK_FAILED, {"reason": "stream_ended", "is_error": True, "error": error})
        return RunOutcome(ok=False, reason="stream_ended", error=error, native_session_id=native)

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """杀掉进程树。codex 执行命令时拉起 PowerShell 子进程，只 kill
        包装进程会留下孤儿；Windows 用 ``taskkill /T``，失败退回 ``kill()``。"""
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=5.0)
                if proc.returncode is not None:
                    return
            except (OSError, ProcessLookupError, RuntimeError, asyncio.TimeoutError, TimeoutError):
                pass
        try:
            proc.kill()
        except (ProcessLookupError, RuntimeError):
            pass
