"""Codex 的 AgentAdapter 实现。

接入对象是 OpenAI Codex CLI（本机随 Codex Desktop 分发，0.162.0-alpha.2
实测）。与其它三家形态差异最大的一家：每回合一个 ``codex exec`` 进程、
事件是自有的 JSONL 方言（不是 Anthropic stream-json 族）、非交互模式
没有权限提示环节。差异全部消化在本子包，核心层只见 AgentAdapter。
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
from ..diagnose import DiagRow
from . import discovery, locate
from .session import CodexSession

#: 本 Adapter 当前具备的能力。
#:
#: ``streaming_events``：``codex exec --json`` 逐条 JSONL 事件，协议探针实测。
#: ``resume``：``codex exec resume <thread_id>`` 实测可用，续轮追加进同一
#: rollout 文件。
#: ``interrupt``：唯一手段是杀进程树（taskkill /T，同 Claude 的结论）。
#: ``permission_gate`` 是 False：非交互 exec **没有权限提示环节**，自主度由
#: 启动期 sandbox 一次性决定，绊线（Policy）没有拦截点可注入——这是已声明
#: 的局限，不是待办。
#: ``cost_reporting``：turn.completed 带 token 用量（input/cached/output/
#: reasoning），**没有 USD 折算**，cost_usd 一律 None。
#: ``history_discovery``：rollout 文件可扫可读（discovery.py）。
CAPABILITIES = AgentCapabilities(
    streaming_events=True,
    resume=True,
    interrupt=True,
    permission_gate=False,
    cost_reporting=True,
    history_discovery=True,
)


class CodexAdapter(AgentAdapter):
    """接入本机 Codex CLI（Desktop 内置或独立安装）。"""

    agent_id = "codex"
    display_name = "Codex"

    @classmethod
    def detect(cls) -> AgentInfo:
        """没装是正常情况，用 MISSING 表达，**不抛异常**。"""
        command = locate.find_command()
        if command is None:
            return AgentInfo(
                id=cls.agent_id,
                name=cls.display_name,
                status=AgentStatus.MISSING,
                detail="没找到 Codex CLI：装 Codex 桌面应用、npm i -g @openai/codex、"
                       "或在 config.toml 配 [codex] command 任选其一。"
                       "模型后端由 ~/.codex/config.toml 决定（走桌面应用本地代理时它需在线）",
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

    @classmethod
    def diagnose_rows(cls) -> list[DiagRow]:
        """doctor 用：查找过程的可打印诊断（可选钩子，doctor 靠 getattr 探测）。"""
        return locate.diagnose()

    async def discover_projects(self, blocked: Sequence[Path] = ()) -> ScanResult:
        """扫 rollout 文件（同步文件 IO，直接在协程里跑，理由同 Claude）。"""
        return discovery.scan(locate.sessions_dir(), blocked, agent_id=self.agent_id)

    @classmethod
    async def read_history(
        cls,
        native_session_id: str,
        limit: int = 60,
        *,
        directory: str | None = None,
    ) -> list[dict] | None:
        """会话正文在 rollout 文件里，解析是 :mod:`discovery` 的事。

        ``directory`` 用不上：文件按会话 uuid 的文件名找，与项目路径无关。
        """
        return discovery.read_history(locate.sessions_dir(), native_session_id, limit)

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
            raise RuntimeError("没找到 Codex CLI，无法创建会话")

        # 这里只建句柄，不启动进程——启动推迟到第一次 send()（每回合一个进程）。
        return CodexSession(
            command=command,
            cwd=Path(cwd),
            task_id=task_id,
            emit=emit,
            autonomy=autonomy,
            resume_from=resume_from,
            policy=policy,
            prompt_url=prompt_url,
        )
