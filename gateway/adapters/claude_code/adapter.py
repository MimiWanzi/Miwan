"""Claude Code 的 AgentAdapter 实现。

这个模块是 Claude Code 与核心层之间**唯一**的接口面。核心层只通过
:class:`~gateway.adapters.base.AgentAdapter` 认识它，不 import 这里的任何具体符号。
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from ..base import (
    AgentAdapter,
    AgentCapabilities,
    AgentInfo,
    AgentSession,
    AgentStatus,
    AutonomyPolicy,
    EmitFn,
    ScanResult,
)
from . import discovery
from .locate import clean_env, find_exe, projects_dir
from ...permissions import Policy
from .session import ClaudeSession

#: 本 Adapter 当前具备的能力。
#:
#: ``permission_gate`` 是 False，但这不是因为 Claude Code 做不到——
#: 它恰恰是三种 Agent 里唯一原生支持运行时权限拦截的（`--permission-prompts host`）。
#: 标 False 是因为**第 6 层还没实现**，而核心层拿这个标志做功能降级判断，
#: 所以它必须反映实际可用性而不是理论能力。第 6 层落地后这里改成 True 即可。
CAPABILITIES = AgentCapabilities(
    streaming_events=True,
    resume=True,
    interrupt=True,
    permission_gate=False,
    cost_reporting=True,
    history_discovery=True,
)

VERSION_TIMEOUT = 20.0


def _run_version(exe: Path) -> tuple[str | None, str | None]:
    """跑 ``claude --version``。返回 ``(版本号, 失败原因)``。

    这是唯一的同步子进程调用，只出现在检测阶段，不在任务路径上。
    """
    try:
        proc = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VERSION_TIMEOUT,
            env=clean_env(),
        )
    except subprocess.TimeoutExpired:
        return None, f"--version 超过 {VERSION_TIMEOUT:g}s 未返回"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"运行 --version 失败：{exc}"

    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    first = output.splitlines()[0].strip() if output else ""
    return (first or None), (None if first else "claude --version 没有输出")


class ClaudeCodeAdapter(AgentAdapter):
    """接入本机安装的 Claude Code CLI。"""

    agent_id = "claude_code"
    display_name = "Claude Code"

    @classmethod
    def detect(cls) -> AgentInfo:
        """没装是正常情况，用 MISSING 表达，**不抛异常**。"""
        exe = find_exe()
        if exe is None:
            return AgentInfo(
                id=cls.agent_id,
                name=cls.display_name,
                status=AgentStatus.MISSING,
                detail="未找到 claude 可执行文件",
                capabilities=CAPABILITIES,
            )

        version, failure = _run_version(exe)
        return AgentInfo(
            id=cls.agent_id,
            name=cls.display_name,
            status=AgentStatus.READY if version else AgentStatus.UNCONFIGURED,
            version=version,
            executable=str(exe),
            detail=failure,
            capabilities=CAPABILITIES,
        )

    async def discover_projects(self, blocked: Sequence[Path] = ()) -> ScanResult:
        """扫本地 JSONL。

        内部是同步文件 IO，直接在协程里跑即可——实测扫描 10 个项目 / 24 个会话
        耗时 0.01s，为这点开销引入线程池不划算。
        将来接 OpenCode 时它这个方法是真异步（走 HTTP），接口已经预留好了。
        """
        return discovery.scan(projects_dir(), blocked, agent_id=self.agent_id)

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
        exe = find_exe()
        if exe is None:
            raise RuntimeError("找不到 claude 可执行文件，无法创建会话")

        # 这里只建句柄，不启动进程——启动推迟到第一次 send()。
        return ClaudeSession(
            exe=exe,
            cwd=Path(cwd),
            task_id=task_id,
            emit=emit,
            autonomy=autonomy,
            resume_from=resume_from,
            policy=policy,
        )
