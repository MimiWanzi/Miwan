"""WorkBuddy 的 AgentAdapter 实现。

WorkBuddy 桌面应用（腾讯）内置的 CodeBuddy Code CLI 就是接入对象——
它说的是 Anthropic 风格 stream-json 方言（解析器与 Claude 共用，
见 ``adapters/anthropic_stream.py``），会话存储布局也与 Claude 同构。
差异都在 Adapter 层消化：node 启动脚本、deny-only 权限、毫秒时间戳的
JSONL 记录格式。
"""

from __future__ import annotations

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
from ...permissions import Policy
from . import discovery, locate
from .session import WorkBuddySession

#: 本 Adapter 当前具备的能力。
#:
#: ``permission_gate`` 是 False：CodeBuddy 没有 ``--permission-prompt-tool``
#: 这类运行时审批旗标（2.156.0 实测 --help 全量核对），EDIT/FULL 档下未被
#: 预批准的工具在无人值守时被静默拒绝、事后汇报（deny-only，同 OpenCode）。
#: ``resume`` 标 True 的依据是 ``--resume <sessionId>`` 旗标存在；跨轮常驻
#: 进程与续接行为在登录后的真跑探针里验证。
CAPABILITIES = AgentCapabilities(
    streaming_events=True,
    resume=True,
    interrupt=True,
    permission_gate=False,
    cost_reporting=True,
    history_discovery=True,
)


class WorkBuddyAdapter(AgentAdapter):
    """接入本机 WorkBuddy 内置的 CodeBuddy CLI。"""

    agent_id = "workbuddy"
    display_name = "WorkBuddy"

    @classmethod
    def detect(cls) -> AgentInfo:
        """没装是正常情况，用 MISSING 表达，**不抛异常**。"""
        command = locate.find_command()
        if command is None:
            return AgentInfo(
                id=cls.agent_id,
                name=cls.display_name,
                status=AgentStatus.MISSING,
                detail="没找到 CodeBuddy CLI：装了 WorkBuddy 桌面端（且机器上有 Node）"
                       "或在 config.toml 配 [workbuddy] command 才能用",
                capabilities=CAPABILITIES,
            )

        version, failure = locate.run_version(command)
        return AgentInfo(
            id=cls.agent_id,
            name=cls.display_name,
            status=AgentStatus.READY if version else AgentStatus.UNCONFIGURED,
            version=version,
            executable=command[0],
            detail=failure,
            capabilities=CAPABILITIES,
        )

    async def discover_projects(self, blocked: Sequence[Path] = ()) -> ScanResult:
        """扫本地 JSONL（同步文件 IO，直接在协程里跑，理由同 Claude）。"""
        return discovery.scan(locate.projects_dir(), blocked, agent_id=self.agent_id)

    @classmethod
    async def read_history(
        cls,
        native_session_id: str,
        limit: int = 60,
        *,
        directory: str | None = None,
    ) -> list[dict] | None:
        """会话正文在自己的 JSONL 里，解析是 :mod:`discovery` 的事。

        ``directory`` 用不上：文件按 session uuid 的文件名找，与项目路径无关。
        """
        return discovery.read_history(locate.projects_dir(), native_session_id, limit)

    def create_session(
        self,
        *,
        cwd: Path,
        task_id: str,
        emit: EmitFn,
        autonomy: AutonomyPolicy = AutonomyPolicy.READ_ONLY,
        resume_from: str | None = None,
        policy: Policy | None = None,
        prompt_url: str | None = None,
    ) -> AgentSession:
        command = locate.find_command()
        if command is None:
            raise RuntimeError("没找到 CodeBuddy CLI，无法创建会话")

        # 这里只建句柄，不启动进程——启动推迟到第一次 send()。
        return WorkBuddySession(
            command=command,
            cwd=Path(cwd),
            task_id=task_id,
            emit=emit,
            autonomy=autonomy,
            resume_from=resume_from,
            policy=policy,
            prompt_url=prompt_url,
        )
