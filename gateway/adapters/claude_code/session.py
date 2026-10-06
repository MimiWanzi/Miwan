"""驱动一个 claude 进程。

实测（探针 P2）：``-p --input-format stream-json`` 下，一个进程可以连续处理多轮，
跨轮上下文保持。所以这里让进程常驻，``send()`` 只是往 stdin 写一行。

**但"常驻"不是接口的一部分。** Codex 的每轮就是一个新进程，同样满足
:class:`~gateway.adapters.base.AgentSession` 的接口。上层不该依赖这个细节，
哪天换成"每轮起进程"也不影响核心层。
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
from ..base import AgentSession, AutonomyPolicy, EmitFn, RunOutcome
from .locate import clean_env
from .normalize import Mapper

#: READ_ONLY 档位下开放的工具。
#: 用**限定工具集**而不是靠权限提示拒绝，是因为前者是确定性的：
#: 无论将来权限通道怎么实现，这批工具都不可能写出任何东西。
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")

#: FULL 档位下**预先批准**的工具。
#:
#: 这份清单是**手工挑的，不是从 CLI 问来的**——所以它有真实的失效模式：
#: 漏掉一个工具，那个工具就会走权限提示，而无人应答时提示等于拒绝，
#: 表现为"某个能力莫名不可用"，且没有任何报错指向这里。
#:
#: 校验办法（照着做，别猜）：`system/init` 里带一个 `tools` 数组，
#: `task_started` 事件已经把它落库了。跑一个 FULL 任务，把那个数组和
#: 这里对一遍，差的补上。剩下的风险是 CLI 升级后新增工具——
#: 那会以同样的方式静默失效，所以定期对一遍是**运维动作**，不是一次性的。
FULL_TOOLS = (
    "Read", "Write", "Edit", "NotebookEdit",
    "Glob", "Grep",
    "Bash",
    "PowerShell",
    "WebFetch", "WebSearch",
    "Task", "TodoWrite",
)
#: 当前 CLI 里 shell 工具的名字是 ``Bash``（Windows 上也是）。历史上这里
#: 只写了 ``PowerShell``，shell 就整体静默失效——CLI 改名/加工具不会报错，
#: 只会走到权限提示然后无人应答。现在有审批通道兜底（EDIT/FULL 档漏掉
#: 的工具会"问人"而不是"拒绝"），但这份清单仍要定期和 system/init 的
#: tools 数组对一遍，理由见模块顶部 FULL_TOOLS 的注释。

#: close() 里"关掉 stdin 等它自己退"的宽限期。
#:
#: 实测（probes/verify_shutdown.py）：**这一招对正在跑一轮的 claude 没用**——
#: 它会把当前这轮做完才看 stdin，所以宽限期必然全部耗光，然后才强杀。
#: 宽限期真正有用的只有"空闲会话"那一种情况，而那种情况本来就是立刻退出的。
#: 所以这个值越小越好：大了只会让 Ctrl-C 像卡住。
#: 缩短它是安全的——第 3 层验证过，中途强杀之后 --resume 照样能接上。
GRACE_EXIT = 2.0


class ClaudeSession(AgentSession):
    """一个 Claude Code 会话。进程在第一次 send() 时才真正拉起来。"""

    def __init__(
        self,
        *,
        exe: Path,
        cwd: Path,
        task_id: str,
        emit: EmitFn,
        autonomy: AutonomyPolicy = AutonomyPolicy.READ_ONLY,
        resume_from: str | None = None,
        budget_usd: float | None = None,
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
        #: 权限审批回调地址（MCP HTTP 服务，见 gateway/mcp.py）。
        #: None = 没有审批通道，权限点维持原样——静默拒绝。
        self._prompt_url = prompt_url
        self._mcp_config_path: Path | None = None
        #: 绊线清单。默认只有内置那 8 条——**不在这里读 data/tripwires.txt**：
        #: 那会让"会话怎么建"和"配置文件在哪"耦合，测试里就没法塞一份临时清单
        #: 进来。谁装配谁负责加载，这里只消费。
        #:
        #: 判 `is None` 而不是 `or`：Policy 有 __len__，空清单是**假值**，
        #: 写成 `policy or Policy()` 会让"我故意把绊线清空"变成"用回默认那 8 条"——
        #: 不报错、没日志，防线悄悄回来了。这个坑是探针里那条空清单用例逮到的。
        self.policy = Policy() if policy is None else policy
        self._exe = exe
        self._budget_usd = budget_usd
        self._proc: asyncio.subprocess.Process | None = None
        self._mapper = Mapper()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._interrupted = False

        #: 是否已经用过这个会话 id（决定下次起进程用 --resume 还是 --session-id）。
        self._resume = resume_from is not None

        # 预分配会话 id（探针 P4 验证可用），且**在构造时就分配**而不是等第一次 send。
        # 这样 runner 一建完会话就知道 native_session_id 能立刻落库——
        # 否则网关在任务跑到一半时崩掉，这个 id 就丢了，那个任务再也接不回去。
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
            # 中断后重起、或用户主动续接历史会话，都走这条路。
            args += ["--resume", self.native_session_id]
        else:
            args += ["--session-id", self.native_session_id]

        if self.autonomy is AutonomyPolicy.READ_ONLY:
            # 限定工具集，不靠权限提示拒绝——前者是确定性的
            args += ["--tools", *READ_ONLY_TOOLS]
        elif self.autonomy is AutonomyPolicy.EDIT:
            args += ["--permission-mode", "acceptEdits"]
        elif self.autonomy is AutonomyPolicy.FULL:
            # 按名放行全部工具（注意是 --allowedTools 不是 --tools：
            # 前者"预先批准"，后者"限定集合"。混用会把工具集砍掉）
            args += ["--allowedTools", *FULL_TOOLS]
            disallowed = [g for t in self.policy.tripwires for g in t.disallowed_tools()]
            if disallowed:
                args += ["--disallowedTools", *disallowed]

        if self._prompt_url and self.autonomy in (
            AutonomyPolicy.EDIT,
            AutonomyPolicy.FULL,
        ):
            # 权限点问手机。绊线不受影响：--disallowedTools 是硬拒绝，
            # 走不到提示这一步——"规则"与"问人"是两层不同的防线。
            args += [
                "--mcp-config", self._write_mcp_config(),
                "--permission-prompt-tool", "mcp__perm__request_permission",
            ]

        if self._budget_usd:
            args += ["--max-budget-usd", str(self._budget_usd)]

        return args

    def _write_mcp_config(self) -> str:
        """把审批通道写成一份临时 MCP 配置，返回文件路径。

        里面只有回调 URL（带 runner 发的一次性密钥），没有别的秘密：
        这份文件会被 CLI 读到，而 CLI 进程本身是任务可控的——所以真正的
        防线不在这份文件，而在「批准」这个动作必须持设备凭证（见
        runner.decide_permission 的调用方）。
        """
        if self._mcp_config_path is not None:
            self._mcp_config_path.unlink(missing_ok=True)
        fd, raw = tempfile.mkstemp(prefix="remote-agent-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "mcpServers": {
                        "perm": {"type": "http", "url": self._prompt_url},
                    }
                },
                handle,
            )
        self._mcp_config_path = Path(raw)
        return raw

    async def _ensure_process(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return

        self._proc = await asyncio.create_subprocess_exec(
            str(self._exe),
            *self._build_args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=clean_env(),
        )
        # 从这个 id 已经被用过的那一刻起，之后任何重启都只能走 --resume——
        # 再用 --session-id 传同一个 id 会被判为重复会话。
        self._resume = True
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """必须持续排空 stderr，否则子进程会卡死。

        实测 stderr 会持续输出 ``[claude-code:unrecognized_model]`` 之类的告警。
        没人读的话管道缓冲区写满，子进程就阻塞在写 stderr 上——
        表现出来是"任务莫名挂住"，而 stdout 一切正常，极难排查。
        留最后若干行，崩溃时拼进错误信息。
        """
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
            # 主动中断和进程崩溃都会让 stdout 到达 EOF，但含义完全不同：
            # 前者是用户按了按钮，后者是出了故障。区分开，否则手机上会把
            # "我取消了"显示成"任务失败"。
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
        """读 stdout 直到 result 消息或流结束。

        result 是**每轮**的结束标志，一个 result 恰好对应一次 send（探针 P2 实测）。

        这里**刻意没有单轮超时**：复杂任务跑二十分钟是正常的，按墙钟时间
        强杀只会误杀还在干活的任务。兜底都在别处——上下文耗尽或出错时
        CLI 自己会收；进程真挂死了，表现是"状态一直是运行中"，由人在
        手机上手动中断；不想给额度上保险的话还有 --max-budget-usd。
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

            for type_, payload in self._mapper.handle(record):
                self._emit(type_, payload)

            if record.get("type") == "result":
                self.native_session_id = self._mapper.native_session_id
                return record

    def _explain_denial(self, payload: dict) -> dict:
        """给"被拒"这个事实补上原因——**是绊线拦的，还是压根没放行**。

        为什么值得单独做：这两种拒绝在数据上一模一样（都是
        ``result.permission_denials`` 里的一条），但对用户完全是两回事。
        绊线拦的说明网关按预期工作了，手机上该显示"它想删目录，我拦下了"；
        没放行的说明我们漏了一个工具，那是**我们的 bug**，
        而且症状是"某个能力莫名不可用"，不查到这里根本想不到。

        实测（probes/out/permissions_run2.txt）确认被拒的工具名是能拿到的，
        所以这里可以拿它回头去问一遍策略。
        """
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
            "reason": "这个工具没有被放行，而无人应答的权限提示等于拒绝——"
                      "很可能是 FULL_TOOLS 漏了它",
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
        """中断当前轮。

        实测确认可用的机制只有"杀进程"这一条。``system/init`` 里虽然报了
        ``interrupt_receipt_v1`` 能力，但那是 SDK 的控制消息协议，裸 CLI 的
        stdin 只接受用户消息格式——发控制消息**未经验证**。拿未验证的协议
        换一点优雅不划算，所以这里用确定能work的方式。

        代价：进程没了，下一轮 send() 会带 ``--resume`` 重起。
        注意"resume + stream-json 输入"这个组合本身还没实测过，
        第一次中断续跑时要留意。
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._interrupted = True
        proc.kill()
        await proc.wait()

    async def close(self) -> None:
        """释放进程。

        关停路径比看起来险：网关被 Ctrl-C 时 ``runner.shutdown()`` 会取消任务，
        取消异常就抛在 ``_execute`` 的 ``finally: await session.close()`` 上。
        如果这时 close 里还有 await（等进程自己退出最多 5 秒），
        **再按一次 Ctrl-C 就会把这次等待也取消掉，进程就留下来了**——
        一个没人看着、继续跑、继续烧额度的 claude。

        所以这里的写法是：任何一条异常路径都要落到"强杀"上，
        ``finally`` 再兜一次底。宁可多杀一次已经退出的进程（空操作），
        也不要漏掉一个活着的。
        """
        if self._closed:
            return
        self._closed = True

        proc = self._proc
        try:
            if proc is not None and proc.returncode is None:
                try:
                    # 关 stdin 让它自己体面退出（探针 P2 验证过空闲会话确实会退）
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
                        await proc.wait()  # 收尸，免得留下僵尸
                    except asyncio.CancelledError:
                        # 已经杀掉了，这里再让取消往外传没有任何意义
                        pass

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

        if self._mcp_config_path is not None:
            self._mcp_config_path.unlink(missing_ok=True)
            self._mcp_config_path = None

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        """杀进程，忽略"它已经死了"这类噪音。"""
        if proc.returncode is not None:
            return
        try:
            proc.kill()
        except (ProcessLookupError, RuntimeError):
            pass
