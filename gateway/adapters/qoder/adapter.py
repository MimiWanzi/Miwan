"""Qoder 的 AgentAdapter 实现。

接入对象是 Qoder CN CLI（``@qodercn-ai/qoderclicn``，1.1.67 实测）。
协议面是四家里与 Claude Code 最接近的一家（Anthropic stream-json 同族），
差异集中在：计费口径（credits 而非 USD）、会话存储（云端而非本地文件）、
权限规则的 CLI 侧表达（只有整工具增删，规则形状无效）。
差异全部消化在本子包，核心层只见 AgentAdapter。
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
from . import locate
from .session import QoderSession

#: 本 Adapter 当前具备的能力（每一条的依据）：
#:
#: ``streaming_events``：``-o stream-json`` 逐条事件，协议与 Claude 同族，
#: 共用 :mod:`gateway.adapters.anthropic_stream` 的 Mapper。
#: ``resume``：``-r <session_id>`` 实测跨进程恢复上下文。
#: ``interrupt``：控制协议 interrupt 实测可用（回执 + error_during_execution），
#: 进程保活，比 Claude 的"杀进程重起"干净。
#: ``permission_gate``：EDIT/FULL 档经 ``--permission-prompt-tool`` 接
#: :mod:`gateway.mcp` 审批通道，越界确认实测路由到 MCP 工具（T7）。
#: ``cost_reporting``：result 带 token 用量与 credits；USD 恒 0，
#: cost_usd 如实 None（credits 走 usage）。
#: ``history_discovery`` 是 False：会话正文在云端、本地零落盘（受控实验），
#: ``--list-sessions`` 也是云端索引且输出非机器可读——扫不了就是扫不了。
CAPABILITIES = AgentCapabilities(
    streaming_events=True,
    resume=True,
    interrupt=True,
    permission_gate=True,
    cost_reporting=True,
    history_discovery=False,
)


class QoderAdapter(AgentAdapter):
    """接入本机 Qoder CN CLI（npm 独立安装）。"""

    agent_id = "qoder"
    display_name = "Qoder"

    @classmethod
    def detect(cls) -> AgentInfo:
        """没装是正常情况，用 MISSING 表达，**不抛异常**。

        登录态用 ``status`` 一次调用拿到（版本号 + 是否登录）——CLI 与
        桌面端凭证不互通，"装了"不等于"能用"，未登录必须在这里点明，
        不能等用户派了任务才撞 "Not logged in"。
        """
        command = locate.find_command()
        if command is None:
            return AgentInfo(
                id=cls.agent_id,
                name=cls.display_name,
                status=AgentStatus.MISSING,
                detail="没找到 Qoder CLI：npm i -g @qodercn-ai/qoderclicn，"
                       "或在 config.toml 配 [qoder] command。",
                capabilities=CAPABILITIES,
            )

        status = locate.run_status(command)
        if status.version is None:
            return AgentInfo(
                id=cls.agent_id,
                name=cls.display_name,
                status=AgentStatus.UNCONFIGURED,
                executable=command[0],
                detail=status.failure or "qodercn status 没有可解析的输出",
                capabilities=CAPABILITIES,
            )
        if not status.logged_in:
            return AgentInfo(
                id=cls.agent_id,
                name=cls.display_name,
                status=AgentStatus.UNCONFIGURED,
                version=status.version,
                executable=command[0],
                detail="CLI 已安装但未登录（与桌面端凭证不互通）："
                       "运行 qodercn login 完成一次浏览器授权。",
                capabilities=CAPABILITIES,
            )
        return AgentInfo(
            id=cls.agent_id,
            name=cls.display_name,
            status=AgentStatus.READY,
            version=status.version,
            executable=command[0],
            capabilities=CAPABILITIES,
        )

    @classmethod
    def diagnose_rows(cls) -> list[DiagRow]:
        """doctor 用：查找过程的可打印诊断（可选钩子，doctor 靠 getattr 探测）。"""
        return locate.diagnose()

    async def discover_projects(self, blocked: Sequence[Path] = ()) -> ScanResult:
        """会话正文在云端，本地无可扫的目录——如实返回空（见 CAPABILITIES）。"""
        return ScanResult()

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
            raise RuntimeError("没找到 Qoder CLI，无法创建会话")

        # 这里只建句柄，不启动进程——启动推迟到第一次 send()。
        return QoderSession(
            command=command,
            cwd=Path(cwd),
            task_id=task_id,
            emit=emit,
            autonomy=autonomy,
            resume_from=resume_from,
            policy=policy,
            prompt_url=prompt_url,
        )
