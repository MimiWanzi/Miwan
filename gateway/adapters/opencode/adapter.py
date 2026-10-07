"""OpenCode 的 AgentAdapter 实现。

与 Claude 的最大差异在 :meth:`discover_projects`：它**走 HTTP**（这正是
``AgentAdapter.discover_projects`` 是 async 的原因），数据来自 serve 的
``/project`` 和 ``/session`` 接口，而不是本地文件。
"""

from __future__ import annotations

import tempfile
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
from ..history import TEXT_LIMIT, tool_detail
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


def _session_of(raw: dict, project_id: str, agent_id: str) -> Session | None:
    """/session 列表里的一条原始记录 → 网关的 Session；不合格式返回 None。"""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    updated = (raw.get("time") or {}).get("updated")
    last = (updated / 1000) if updated else None
    title = str(raw.get("title") or "(无标题)")[:120]
    return Session(
        id=f"{project_id}:{raw['id']}",
        project_id=project_id,
        agent_id=agent_id,
        native_session_id=raw["id"],
        title=title,
        last_active_at=last,
    )


def _in_temp(directory: str) -> bool:
    """目录是否在系统临时目录里——探针的一次性会话都在那，不还原成项目。"""
    try:
        Path(directory).relative_to(Path(tempfile.gettempdir()))
        return True
    except ValueError:
        return False


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

        几个实测确认的细节：

        - 会话按 ``project_id`` 归属项目，``GET /session?directory=X`` 默认还要求
          ``directory`` 精确匹配——在项目子目录里跑的会话会被漏掉。带
          ``scope=project`` 才是"这个项目的全部会话"（2026-10-06，1.18.28）。
        - OpenCode 的项目身份来自 git（remote → 本地缓存 → 首个提交，见它源码
          ``project.resolve()``）：非 git 目录、或 ``git init`` 了还没有任何提交的
          全新项目，会话一律落进 ``global`` 兜底桶。网页端新建的项目就处在
          这个状态——所以 global 桶不能跳过，要按会话的 ``directory`` 分组
          还原成项目（见 :meth:`_global_projects`），否则用户永远看不到它们。
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

        await self._global_projects(http, base, is_blocked, result)

        result.projects.sort(key=lambda p: p.last_active_at or 0, reverse=True)
        return result

    async def _global_projects(
        self,
        http: aiohttp.ClientSession,
        base: str,
        is_blocked: BlockFilter,
        result: ScanResult,
    ) -> None:
        """把 global 兜底桶里的会话按 ``directory`` 分组，还原成项目。

        ``GET /session?directory=/&scope=project``：``/`` 解析不到 git 仓库，
        必然落在 global 桶，于是拿到的是"全部未归项目的会话"。每个会话
        自带 ``directory``，按它分组就是"哪些目录里有会话"。

        Temp 目录跳过：探针的一次性会话都在这，列出来全是噪音。
        """
        try:
            async with http.get(
                f"{base}/session?directory=%2F&scope=project&limit=500"
            ) as resp:
                if resp.status != 200:
                    print(
                        f"[opencode] 拉取 global 会话失败（HTTP {resp.status}），"
                        "未注册目录的项目本次缺失",
                        flush=True,
                    )
                    return
                raw_sessions = await resp.json()
        except aiohttp.ClientError as exc:
            print(f"[opencode] 拉取 global 会话失败：{exc}", flush=True)
            return

        groups: dict[str, list[dict]] = {}
        for raw in raw_sessions if isinstance(raw_sessions, list) else []:
            if not isinstance(raw, dict):
                continue
            directory = str(raw.get("directory") or "")
            if not directory or _in_temp(directory):
                continue
            groups.setdefault(directory, []).append(raw)

        for directory, raws in groups.items():
            if is_blocked(directory):
                result.skipped_blocked.append(directory)
                continue
            project_id = stable_id(directory)
            sessions = [
                s for s in (_session_of(raw, project_id, self.agent_id) for raw in raws) if s
            ]
            if not sessions:
                continue
            sessions.sort(key=lambda s: s.last_active_at or 0, reverse=True)
            newest = max((s.last_active_at or 0.0) for s in sessions)
            result.projects.append(
                Project(
                    id=project_id,
                    name=Path(directory).name or directory,
                    path=directory,
                    git_branch=None,
                    last_active_at=newest or None,
                    session_count=len(sessions),
                )
            )
            result.sessions[project_id] = sessions

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
            session = _session_of(raw, project_id, self.agent_id)
            if session is None:
                continue
            newest = max(newest, session.last_active_at or 0.0)
            sessions.append(session)
        sessions.sort(key=lambda s: s.last_active_at or 0, reverse=True)
        return sessions, newest

    @classmethod
    async def read_history(
        cls,
        native_session_id: str,
        limit: int = 60,
        *,
        directory: str | None = None,
    ) -> list[dict] | None:
        """问 serve 要会话正文（``GET /session/:id/message``），归一化成消息条目。

        directory 是必须的：serve 的接口按目录隔离，没它定位不到会话。
        拿不到（None/空）就返回 None，与"不支持"同待遇——调用方不该在
        没有项目路径的情况下走到这里。

        serve 是惰性启动的：为了读一次历史可能要等它冷启动几秒。
        discover_projects 已经接受这个代价（整条历史链路都靠它），
        读历史没有理由更娇气。
        """
        if not directory:
            return None
        try:
            http, base = await serve.manager.acquire()
            quoted = urllib.parse.quote(directory, safe="")
            async with http.get(
                f"{base}/session/{urllib.parse.quote(native_session_id, safe='')}"
                f"/message?directory={quoted}"
            ) as resp:
                if resp.status != 200:
                    # 404 多半是会话被用户清了；其他状态码也按"没有正文"处理，
                    # 历史页显示空列表比 500 报错更接近事实
                    return []
                raw = await resp.json()
        except (RuntimeError, OSError, aiohttp.ClientError) as exc:
            print(f"[opencode] 读会话正文失败（serve 不可用？）：{exc}", flush=True)
            return []

        messages: list[dict] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            info = item.get("info")
            if not isinstance(info, dict):
                continue
            role = info.get("role")
            if role not in ("user", "assistant"):
                continue
            created = (info.get("time") or {}).get("created")
            ts = (created / 1000) if isinstance(created, (int, float)) else None
            for part in item.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text = (part.get("text") or "").strip()
                    if text:
                        messages.append({
                            "role": role,
                            "text": text[:TEXT_LIMIT],
                            "ts": ts,
                        })
                elif ptype == "tool" and role == "assistant":
                    state = part.get("state") or {}
                    messages.append({
                        "role": role,
                        "tool": part.get("tool") or "?",
                        "detail": tool_detail(state.get("input")),
                        "ts": ts,
                    })
        return messages[-limit:] if limit > 0 else []

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
