"""驱动 Qoder CLI 的常驻进程。

进程模型与 Claude Code 相同（实测）：``-p --input-format stream-json``
下常驻进程连续处理多轮、跨轮上下文保持，session_id 跨轮稳定；每轮开头
CLI 重发一条 system/init（Mapper 的去重保证 task_started 每轮恰好一条）。

权限模型（无头 ``-p`` 模式实测，探针记录 T1–T14 见项目记忆）：

  - ``default`` 与 ``accept_edits`` 无头下行为一致：工作区内自动放行，
    越界操作 tool_result 报 "requires confirmation"。该确认可经
    ``--permission-prompt-tool`` 路由到网关的 MCP 审批通道（实测打通，
    载荷与 Claude 同约定：tool_name / input / tool_use_id）——所以
    permission_gate=True，EDIT/FULL 档接 :mod:`gateway.mcp`。
  - ``dont_ask`` 自动拒绝（记 permission_denials）；``bypass_permissions``
    全放行——等价 codex 的 danger-full-access，红线不用。
  - ``--tools`` 硬裁工具集。它是可变参数旗标、会吞掉后面的位置参数，
    所以参数顺序上它后面只允许跟 dash 旗标（本模块 _build_args 保证）。
  - ``--allowed-tools`` / ``--disallowed-tools`` 只能整工具增删（规则形状
    如 ``Bash(rm*)`` 不会被解析成规则），且预批不了越界操作——绊线的
    「规则」半边因此落在网关侧：MCP 门进来先过 Policy（runner 内），
    CLI 侧没有可用的规则表达。
  - CLI 自带两道网：工作区边界（越界写读要确认）与危险命令检测
    （工作区内 ``rm -rf`` 也拦），与本平台的档位/绊线互补。

会话存储：正文在云端（受控实验本地零落盘），``--list-sessions`` 也是
云端索引——本地扫描式 discovery 不成立，history_discovery=False；
续接用 ``-r <session_id>``（实测跨进程恢复上下文）。

计费：credits 而非 USD——``total_cost_usd`` 恒 0，``total_credits`` 才是
真实消耗。cost_usd 如实 None，credits 塞进完成事件的 usage。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections import deque
from pathlib import Path

from ...events import EventType
from ...permissions import Policy
from ..anthropic_stream import Mapper
from ..base import AgentSession, AutonomyPolicy, EmitFn, RunOutcome
from .locate import clean_env

#: READ_ONLY 档开放的工具。确定性收缩（--tools 直接从工具集里拿走其余）：
#: 无论权限通道是否有人应答，这批工具都写不出任何东西。
READ_ONLY_TOOLS = (
    "Read", "Glob", "Grep",
    "WebFetch", "WebSearch", "ImageSearch",
    "TaskGet", "TaskList", "GetGoal",
)

#: EDIT 档 = READ_ONLY + 写文件类。**刻意不含 Bash/Agent/Workflow**：
#: Qoder 的档位引擎区分不了"改文件"和"跑命令"（工作区内两者都自动放行），
#: EDIT 的"不能执行任意命令"只能靠工具集收缩来保证——这正好也是
#: 确定性路径。跨 Agent 语义对齐：Claude 的 EDIT 档同样跑不了任意命令。
EDIT_TOOLS = (
    *READ_ONLY_TOOLS,
    "Write", "Edit", "NotebookEdit",
    "TaskCreate", "TaskStop", "TaskUpdate",
)

#: FULL 档**不传 --tools**（全部工具可用）。与 Claude 不同：Claude 的
#: FULL 要手工挑 --allowedTools 预批清单，Qoder 无头模式的默认行为就是
#: "工作区内自动放行 + 越界走审批门"，不存在"漏一个工具就静默不可用"
#: 的失效模式，也就不需要维护那份会腐化的清单。

#: Qoder 每轮固定发、但对我们零信息量的 system 子类型。
#: Mapper 对未知子类型降级 RAW，不在这里滤掉的话事件流全是这种噪音。
NOISE_SYSTEM_SUBTYPES = frozenset({"artifacts_update", "hook_started", "hook_response"})

#: 会话层自有的控制协议回执（interrupt 的应答）。它只对驱动进程的一方
#: 有意义——中断的结局由该轮的 result（error_during_execution）承载，
#: 回执本身不该出现在用户事件流里。
NOISE_TOP_TYPES = frozenset({"control_response"})

#: close() 里"关掉 stdin 等它自己退"的宽限期（同 Claude 的取值理由）。
GRACE_EXIT = 2.0


class QoderSession(AgentSession):
    """一个 Qoder 会话。进程在第一次 send() 时才真正拉起来。"""

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
        self._prompt_url = prompt_url
        self._mcp_config_path: Path | None = None
        # 判 `is None` 而不是 `or`：空清单是假值，`or` 会把"故意清空绊线"
        # 变成"用回默认清单"（同 Claude 的坑）。
        self.policy = Policy() if policy is None else policy
        self._proc: asyncio.subprocess.Process | None = None
        self._mapper = Mapper()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._interrupted = False
        self._resume = resume_from is not None

        # 预分配会话 id（--session-id 实测可用），构造时就定——runner 建完
        # 会话立刻能落库，网关中途崩掉任务也接得回去。
        if self.native_session_id is None:
            self.native_session_id = str(uuid.uuid4())

    # ------------------------------------------------------------------
    # 进程生命周期
    # ------------------------------------------------------------------

    def _build_args(self) -> list[str]:
        args = [
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
        ]

        if self._resume:
            args += ["--resume", self.native_session_id or ""]
        else:
            args += ["--session-id", self.native_session_id or ""]

        if self.autonomy is AutonomyPolicy.READ_ONLY:
            args += ["--tools", *READ_ONLY_TOOLS]
        elif self.autonomy is AutonomyPolicy.EDIT:
            args += ["--tools", *EDIT_TOOLS]
            args += ["--permission-mode", "accept_edits"]
        elif self.autonomy is AutonomyPolicy.FULL:
            args += ["--permission-mode", "default"]

        if self._prompt_url and self.autonomy in (
            AutonomyPolicy.EDIT,
            AutonomyPolicy.FULL,
        ):
            # READ_ONLY 不接门：工具集收缩已是硬保证，越界确认在无头模式
            # 也只会被拒（没有门时 "requires confirmation" 落成 tool_result
            # 错误），装上门反而让只读任务有弹审批的机会。
            args += [
                "--mcp-config", self._write_mcp_config(),
                "--permission-prompt-tool", "mcp__perm__request_permission",
            ]

        return args

    def _write_mcp_config(self) -> str:
        """把审批通道写成临时 MCP 配置（同 Claude 的做法与理由）。"""
        if self._mcp_config_path is not None:
            self._mcp_config_path.unlink(missing_ok=True)
        fd, raw = tempfile.mkstemp(prefix="miwan-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"mcpServers": {"perm": {"type": "http", "url": self._prompt_url}}},
                handle,
            )
        self._mcp_config_path = Path(raw)
        return raw

    async def _ensure_process(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return

        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            *self._build_args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=clean_env(),
        )
        # 会话 id 已用过：之后任何重启都只能走 --resume（同 Claude 的结论）
        self._resume = True
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """必须持续排空 stderr，否则子进程可能阻塞在写 stderr 上（同 Claude）。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._stderr_tail.append(line)

    # ------------------------------------------------------------------
    # AgentSession 接口
    # ------------------------------------------------------------------

    async def send(self, prompt: str) -> RunOutcome:
        if self._closed:
            raise RuntimeError("会话已关闭")
        await self._ensure_process()
        proc = self._proc
        assert proc is not None and proc.stdin is not None

        self._mapper.begin_turn(prompt)
        self._interrupted = False
        payload = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        }
        try:
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError) as exc:
            self._mapper.end_turn()
            error = f"写入 stdin 失败，进程可能已退出：{exc}"
            self._emit(EventType.TASK_FAILED, {"reason": "stdin_closed", "error": error})
            return RunOutcome(
                ok=False,
                reason="stdin_closed",
                error=error,
                native_session_id=self.native_session_id,
            )

        try:
            result = await self._pump_until_result()
        finally:
            self._mapper.end_turn()

        if result is None:
            if self._interrupted:
                self._interrupted = False
                error = "本轮被主动中断"
                self._emit(EventType.TASK_FAILED, {"reason": "cancelled", "error": error})
                return RunOutcome(
                    ok=False, reason="cancelled", error=error,
                    native_session_id=self.native_session_id,
                )
            tail = "；".join(self._stderr_tail) or "（stderr 无输出）"
            error = f"事件流在收到 result 之前就结束了，进程可能已崩溃。stderr: {tail}"
            self._emit(EventType.TASK_FAILED, {"reason": "stream_ended", "error": error})
            return RunOutcome(
                ok=False, reason="stream_ended", error=error,
                native_session_id=self.native_session_id,
            )

        return self._finish(result)

    async def _pump_until_result(self) -> dict | None:
        """读 stdout 直到 result 记录或流结束（同 Claude 的每轮一条 result）。

        刻意没有单轮超时（同 Claude）：复杂任务跑很久是正常的，墙钟强杀
        只会误杀；进程真挂死由人在手机上手动中断。
        """
        proc = self._proc
        assert proc is not None and proc.stdout is not None

        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return None
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                self._emit(EventType.RAW, {"reason": "非 JSON 输出行", "line": line[:2000]})
                continue
            if not isinstance(record, dict):
                continue

            if record.get("type") in NOISE_TOP_TYPES:
                continue
            if record.get("type") == "system" and record.get("subtype") in NOISE_SYSTEM_SUBTYPES:
                continue

            for type_, payload in self._mapper.handle(record):
                self._emit(type_, payload)

            if record.get("type") == "result":
                self.native_session_id = self._mapper.native_session_id
                return record

    def _finish(self, result: dict) -> RunOutcome:
        subtype = str(result.get("subtype") or "")

        # 中断的落点（实测）：interrupt 回执后该轮以 error_during_execution
        # 收尾。以事实为准——万一 kill/中断落地前 result=success 已刷出来，
        # 照常报完成（同 Codex 的优先级）。
        if self._interrupted and subtype != "success":
            self._interrupted = False
            error = "本轮被主动中断"
            self._emit(EventType.TASK_FAILED, {"reason": "cancelled", "is_error": True, "error": error})
            return RunOutcome(
                ok=False, reason="cancelled", error=error,
                native_session_id=self.native_session_id,
            )

        ok, reason, events = self._mapper.outcome(result)
        for type_, payload in events:
            if type_ in (EventType.TASK_COMPLETED, EventType.TASK_FAILED):
                # 计费口径（见模块 docstring）：USD 恒 0 是形状不是事实，
                # 如实给 None；credits 才是真实消耗。
                payload["cost_usd"] = None
                payload["credits"] = result.get("total_credits")
            self._emit(type_, payload)

        return RunOutcome(
            ok=ok,
            reason=reason,
            error=None if ok else (result.get("result") or result.get("error")),
            cost_usd=None,
            duration_ms=result.get("duration_ms"),
            native_session_id=self.native_session_id,
        )

    async def interrupt(self) -> None:
        """中断当前轮：控制协议发 interrupt（实测回执后该轮以
        error_during_execution 收尾），进程保活可继续用。

        进程已死时无事可做——send() 会自然收到 EOF 走 stream_ended。
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        if proc.stdin is None:
            return
        self._interrupted = True
        request = {
            "type": "control_request",
            "request_id": f"intr-{uuid.uuid4().hex[:8]}",
            "request": {"subtype": "interrupt"},
        }
        try:
            proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
            # 写不进去说明进程已经没了，交回 send() 的 EOF 路径处理
            self._interrupted = False

    async def close(self) -> None:
        """释放进程。写法与 Claude 相同：任何异常路径都要落到强杀上，
        否则 Ctrl-C 两次会留下一个没人看着、继续烧 credits 的 node 进程。
        Qoder 执行命令时会拉起 shell 子进程，杀要连树一起（同 Codex）。"""
        if self._closed:
            return
        self._closed = True

        proc = self._proc
        try:
            if proc is not None and proc.returncode is None:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                    await asyncio.wait_for(proc.wait(), timeout=GRACE_EXIT)
                except (TimeoutError, asyncio.TimeoutError, ProcessLookupError, RuntimeError):
                    pass
        finally:
            if proc is not None:
                await self._kill_tree(proc)
                if proc.returncode is None:
                    try:
                        await proc.wait()
                    except asyncio.CancelledError:
                        pass

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

        if self._mcp_config_path is not None:
            self._mcp_config_path.unlink(missing_ok=True)
            self._mcp_config_path = None

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """杀进程树。node → shell 子进程只 kill 包装进程会留孤儿；
        Windows 用 ``taskkill /T``，失败退回 ``kill()``（同 Codex）。"""
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
