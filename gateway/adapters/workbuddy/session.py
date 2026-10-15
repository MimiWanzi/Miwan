"""驱动一个 CodeBuddy（node 启动脚本）进程。

协议与 Claude Code 的 stream-json 同族（system/init、assistant、user、result），
进程模型也照搬：``-p --input-format stream-json`` 下进程常驻、``send()``
往 stdin 写一行。CodeBuddy 是否同样支持"常驻多轮"**未实测**——若它每轮
退出，下一次 send() 会经 ``_ensure_process`` 带 ``--resume`` 重起，行为仍然
正确，只是每轮多付一次冷启动。登录后的探针（task_started 的 tools 数组、
跨轮上下文）会把这个疑问关掉。

权限模型（2.161.4 实测）：无人值守时 CLI 会以 **SDK 控制协议**索要工具
许可——stdout 出现 ``control_request``（subtype=can_use_tool，形状与
Claude Code 同构），等客户端应答，**不应答就无限等待**（E2E 曾因此挂死
300s，误诊过网络）。本会话的策略是 **deny-only 主动拒绝**：读到
can_use_tool 就回 ``control_response``（behavior=deny，实测 turn 正常
走完、模型如实转述拒绝），同时发一条 PERMISSION_REQUEST 事件（无
permission_id，手机上看得见原因）。``result.permission_denials`` 仍会
事后汇总。permission_gate=False——没有"等人批准"的通道，只有"自动拒绝"。

旗标形状（2.161.4 官方文档核实，曾传错）：三个旗标形状**不同**——
``--tools`` 是逗号分隔单值（``"Bash,Read"``）；``--allowedTools`` /
``--disallowedTools`` 是**空格分隔多值**，每条规则一个独立 argv 项
（``"Bash(git log:*)" "Read"``）。曾把逗号串传给 --allowedTools：整串
被当成一条字面规则、匹配不到任何工具，allowlist 全体失效、所有工具都走
控制协议被 deny——而 E2E 仍是绿的（暗号写在 prompt 里、写文件靠
acceptEdits，都绕开了 allowlist），"绿"掩盖了失效，用户实跑 FULL 档
Bash 被拒才暴露。bundled 2.156.0 作兜底被拉起时旗标兼容性未逐项验证，
但 deny 应答兜底保证最坏结果是"多拒绝"而不是"多放行"。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from pathlib import Path

from ...events import EventType
from ...permissions import Policy
from ..anthropic_stream import Mapper
from ..base import AgentSession, AutonomyPolicy, EmitFn, RunOutcome
from .locate import clean_env

#: READ_ONLY 档位下开放的工具。这三个名字在 CodeBuddy 的 init 工具数组里
#: 实测存在（2.156.0），语义与 Claude 的同名工具一致。
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")

#: FULL 档位下**预先批准**的工具。
#:
#: 手工精选，**刻意小于** CodeBuddy 的完整工具面：它的 init 数组里还有
#: ImageGen / VideoGen / WeChatReply / WeComReply / SendMessage / TeamCreate
#: 这类非编码或高外溢工具（能发微信消息、造子代理），"FULL 档跑编码任务"
#: 不应该等于"这些也放行"。没进清单的工具走 deny-only 静默拒绝，
#: 事后出现在 permission_denials 里——手机上看得见原因。
#:
#: 校验办法同 Claude 的 FULL_TOOLS：task_started 事件落库了 init 的 tools
#: 数组，跑一个 FULL 任务对一遍；CLI 升级后新增工具会以"莫名不可用"的
#: 方式静默失效，定期对一遍是**运维动作**。
FULL_TOOLS = (
    "Read", "Write", "Edit",
    "Glob", "Grep",
    "Bash",
    "PowerShell",
    "WebFetch", "WebSearch",
    "Agent",
    "TaskCreate", "TaskGet", "TaskUpdate", "TaskList", "TaskStop", "TaskOutput",
    "Skill",
)

#: close() 里"关掉 stdin 等它自己退"的宽限期（理由同 Claude 的 GRACE_EXIT）。
GRACE_EXIT = 2.0


class WorkBuddySession(AgentSession):
    """一个 WorkBuddy（CodeBuddy CLI）会话。进程在第一次 send() 时才拉起来。"""

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
        # prompt_url 收下但不用：CodeBuddy 没有权限提示通道（见模块 docstring），
        # 没有可以指向它的钩子。
        self.policy = Policy() if policy is None else policy
        self._proc: asyncio.subprocess.Process | None = None
        self._mapper = Mapper()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._interrupted = False

        self._resume = resume_from is not None

        # 预分配会话 id（--session-id 实测被接受，见 init 记录的 session_id 回显），
        # 构造时就分配——网关中途崩掉这个 id 也已经落库，任务接得回去。
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
            "--verbose",
        ]

        if self._resume:
            args += ["--resume", self.native_session_id]
        else:
            args += ["--session-id", self.native_session_id]

        if self.autonomy is AutonomyPolicy.READ_ONLY:
            # 真实边界 = --allowedTools 预批（实测生效）+ 控制协议 deny 兜底。
            # --tools 照传（逗号串是它的正确形状）但**别指望它**：2.161.4 上
            # init 的 tools 数组不因它变化，留作对 2.156.0/未来版本的尽力边界。
            args += ["--tools", ",".join(READ_ONLY_TOOLS),
                     "--allowedTools", *READ_ONLY_TOOLS]
        elif self.autonomy is AutonomyPolicy.EDIT:
            # acceptEdits 预批文件编辑（实测 Write 直接过）；读类工具显式
            # 预批。Bash/网络类不预批——走控制协议被 deny，模型会如实转述。
            args += ["--permission-mode", "acceptEdits",
                     "--allowedTools", *READ_ONLY_TOOLS]
        elif self.autonomy is AutonomyPolicy.FULL:
            # 按名放行（--allowedTools 是"预先批准"，--tools 是"限定集合"，
            # 混用会把工具集砍掉）
            args += ["--allowedTools", *FULL_TOOLS]
            disallowed = [g for t in self.policy.tripwires for g in t.disallowed_tools()]
            if disallowed:
                args += ["--disallowedTools", *disallowed]

        return args

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
        # 从这个 id 被用过的那一刻起，重启只能走 --resume
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
        payload = {"type": "user", "message": {"role": "user", "content": prompt}}
        try:
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
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

        if result is None and self._interrupted:
            self._interrupted = False
            error = "本轮被主动中断"
            self._emit(EventType.TASK_FAILED, {"reason": "cancelled", "error": error})
            return RunOutcome(
                ok=False,
                reason="cancelled",
                error=error,
                native_session_id=self.native_session_id,
            )

        if result is None:
            tail = "；".join(self._stderr_tail) or "（stderr 无输出）"
            error = f"事件流在收到 result 之前就结束了，进程可能已崩溃。stderr: {tail}"
            self._emit(EventType.TASK_FAILED, {"reason": "stream_ended", "error": error})
            return RunOutcome(
                ok=False,
                reason="stream_ended",
                error=error,
                native_session_id=self.native_session_id,
            )

        return self._finish(result)

    async def _pump_until_result(self) -> dict | None:
        """读 stdout 直到 result 消息或流结束（result 恰好对应一次 send）。

        刻意没有单轮超时，理由同 Claude：复杂任务跑二十分钟是正常的，
        兜底在人在手机上手动中断。
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
                self._emit(
                    EventType.RAW,
                    {"reason": "非 JSON 输出行", "line": line[:2000]},
                )
                continue
            if not isinstance(record, dict):
                continue

            if record.get("type") == "control_request":
                # SDK 控制协议：不应答 CLI 会无限等待（挂死根源），必须先应答
                await self._answer_control_request(record)
                continue

            for type_, payload in self._mapper.handle(record):
                self._emit(type_, payload)

            if record.get("type") == "result":
                self.native_session_id = self._mapper.native_session_id
                return record

    async def _answer_control_request(self, record: dict) -> None:
        """应答 SDK 控制协议。can_use_tool 一律 deny（deny-only）；
        认不出的子类型回 error 应答——总之不能让它等。"""
        proc = self._proc
        request_id = record.get("request_id")
        request = record.get("request") or {}
        if proc is None or proc.stdin is None or not request_id:
            return

        if request.get("subtype") == "can_use_tool":
            payload = {
                "tool_name": request.get("tool_name"),
                "tool_input": request.get("input"),
                "decision": "deny",
                # 与 result.permission_denials 的事后汇总区分开（同一动作两条）
                "via": "control_protocol",
            }
            self._emit(EventType.PERMISSION_REQUEST, self._explain_denial(payload))
            response = {
                "type": "control_response",
                "response": {
                    "request_id": request_id,
                    "subtype": "success",
                    "response": {
                        "behavior": "deny",
                        "message": "Miwan 网关无人值守模式：未放行的操作一律拒绝",
                    },
                },
            }
        else:
            response = {
                "type": "control_response",
                "response": {
                    "request_id": request_id,
                    "subtype": "error",
                    "error": f"未支持的 control 请求：{request.get('subtype')}",
                },
            }

        try:
            proc.stdin.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass

    def _explain_denial(self, payload: dict) -> dict:
        """给"被拒"补上原因——绊线拦的，还是压根没放行（FULL_TOOLS 漏了它）。"""
        verdict = self.policy.decide(
            str(payload.get("tool_name") or ""), payload.get("tool_input")
        )
        if verdict.tripwire is not None:
            return {
                **payload,
                "source": "tripwire",
                "reason": verdict.reason,
                "tripwire": verdict.tripwire.as_dict(),
            }
        return {
            **payload,
            "source": "not_allowed",
            "reason": "该工具未在当前档位放行，网关按 deny-only 策略自动拒绝"
                      "（不是绊线；要放行请调整任务自主档位或 FULL_TOOLS）",
        }

    def _finish(self, result: dict) -> RunOutcome:
        ok, reason, events = self._mapper.outcome(result)
        for type_, payload in events:
            if type_ is EventType.PERMISSION_REQUEST:
                payload = self._explain_denial(payload)
            self._emit(type_, payload)

        return RunOutcome(
            ok=ok,
            reason=reason,
            error=None if ok else (result.get("result") or result.get("error")),
            cost_usd=result.get("total_cost_usd"),
            duration_ms=result.get("duration_ms"),
            native_session_id=self.native_session_id,
        )

    async def interrupt(self) -> None:
        """中断当前轮——唯一确定可用的机制就是杀进程（同 Claude 的结论）。"""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._interrupted = True
        proc.kill()
        await proc.wait()

    async def close(self) -> None:
        """释放进程。任何异常路径都要落到强杀上（详细推演见 Claude 的 close）。"""
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
                self._kill(proc)
                if proc.returncode is None:
                    try:
                        await proc.wait()
                    except asyncio.CancelledError:
                        pass

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.kill()
        except (ProcessLookupError, RuntimeError):
            pass
