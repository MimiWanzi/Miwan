"""OpenCode 的 AgentAdapter 实现。

与 Claude 的最大差异在 :meth:`discover_projects`：它**走 HTTP**（这正是
``AgentAdapter.discover_projects`` 是 async 的原因），数据来自 serve 的
``/project`` 和 ``/session`` 接口，而不是本地文件。
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Sequence
from pathlib import Path

import aiohttp

from ...models import Project, Session, stable_id
from ...permissions import Policy
from ..base import (
    AgentAdapter,
    AgentCapabilities,
    AgentInfo,
    AgentSession,
    AgentStatus,
    AutonomyPolicy,
    BlockFilter,
    EmitFn,
    ScanResult,
)
from . import discovery, serve
from .session import OpenCodeSession

CAPABILITIES = AgentCapabilities(
    streaming_events=True,
    resume=True,
    interrupt=True,
    permission_gate=False,
    cost_reporting=True,
    history_discovery=True,
)


class OpenCodeAdapter(AgentAdapter):
    """接入本机的 OpenCode（经 ``opencode serve`` 的 v1 HTTP API）。"""

    agent_id = "opencode"
    display_name = "OpenCode"

    @classmethod
    def detect(cls) -> AgentInfo:
        """没装是正常情况，用 MISSING 表达，**不抛异常**。"""
        command = discovery.find_command()
        if command is None:
            return AgentInfo(
                id=cls.agent_id,
                name=cls.display_name,
                status=AgentStatus.MISSING,
                detail="PATH 上没有 opencode，config.toml 也没配 [opencode] command",
                capabilities=CAPABILITIES,
            )

        version, failure = discovery.run_version(command)
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
        """问 serve 要项目与会话列表。

        两个实测确认的细节（2026-10-06，1.18.28）：

        - 会话按 ``project_id`` 归属项目，``GET /session?directory=X`` 默认还要求
          ``directory`` 精确匹配——在项目子目录里跑的会话会被漏掉。带
          ``scope=project`` 才是"这个项目的全部会话"。
        - 不是 git 仓库的目录一律挂到 ``worktree="/"`` 的 ``global`` 兜底项目下，
          它的会话散落在任意目录里、没法按项目枚举。跳过它：探针的一次性目录、
          主目录的随手记录都是这里的常客，列出来全是噪音。
        """
        result = ScanResult()
        try:
            http, base = await serve.manager.acquire()
            async with http.get(f"{base}/project") as resp:
                if resp.status != 200:
                    print(
                        f"[opencode] 拉取项目列表失败（HTTP {resp.status}），本次扫描跳过",
                        flush=True,
                    )
                    return result
                raw_projects = await resp.json()
        except (RuntimeError, OSError, aiohttp.ClientError) as exc:
            print(f"[opencode] serve 不可用，本次扫描跳过：{exc}", flush=True)
            return result

        is_blocked = BlockFilter(blocked)
        for raw in raw_projects if isinstance(raw_projects, list) else []:
            if not isinstance(raw, dict):
                continue
            worktree = str(raw.get("worktree") or "")
            if not worktree or worktree in ("/", "\\"):
                continue
            if is_blocked(worktree):
                result.skipped_blocked.append(worktree)
                continue

            project_id = stable_id(worktree)
            sessions, newest_session = await self._sessions_of(http, base, worktree, project_id)
            if not sessions:
                continue

            updated = (raw.get("time") or {}).get("updated")
            result.projects.append(
                Project(
                    id=project_id,
                    name=Path(worktree).name or worktree,
                    path=worktree,
                    git_branch=None,
                    last_active_at=(updated / 1000) if updated else (newest_session or None),
                    session_count=len(sessions),
                )
            )
            result.sessions[project_id] = sessions

        result.projects.sort(key=lambda p: p.last_active_at or 0, reverse=True)
        return result

    async def _sessions_of(
        self,
        http: aiohttp.ClientSession,
        base: str,
        worktree: str,
        project_id: str,
    ) -> tuple[list[Session], float]:
        directory = urllib.parse.quote(worktree, safe="")
        try:
            async with http.get(
                f"{base}/session?directory={directory}&scope=project"
            ) as resp:
                if resp.status != 200:
                    return [], 0.0
                raw_sessions = await resp.json()
        except aiohttp.ClientError:
            return [], 0.0

        sessions: list[Session] = []
        newest = 0.0
        for raw in raw_sessions if isinstance(raw_sessions, list) else []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            updated = (raw.get("time") or {}).get("updated")
            last = (updated / 1000) if updated else None
            newest = max(newest, last or 0.0)
            title = str(raw.get("title") or "(无标题)")[:120]
            sessions.append(
                Session(
                    id=f"{project_id}:{raw['id']}",
                    project_id=project_id,
                    agent_id=self.agent_id,
                    native_session_id=raw["id"],
                    title=title,
                    last_active_at=last,
                )
            )
        sessions.sort(key=lambda s: s.last_active_at or 0, reverse=True)
        return sessions, newest

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
        # prompt_url 收下但不用：OpenCode 的权限是会话级规则集决定的（见
        # session._ruleset），没有"运行时问人"这个环节。
        return OpenCodeSession(
            cwd=Path(cwd),
            task_id=task_id,
            emit=emit,
            autonomy=autonomy,
            resume_from=resume_from,
            policy=policy,
        )

    @classmethod
    async def adapter_shutdown(cls) -> None:
        """网关关停时杀掉共享的 serve 进程。

        Windows 上子进程不随父进程退出，不杀就是孤儿进程，
        持续占着端口和内存。
        """
        await serve.manager.shutdown()
