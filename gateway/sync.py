"""扫描结果 → 数据库的对账。

单独成模块的原因：它同时依赖 :class:`~gateway.store.Store`（存储）和
:class:`~gateway.adapters.base.ScanResult`（Agent 发现）。放在任何一边，
都会让那一层认识本不该认识的东西。

三个入口按粒度排开，CLI 与 HTTP 都从这里取，不各自实现一遍：

    discover()  扫一遍，合并，**不写库**
    refresh()   discover + 写库
    merge() / persist()  更底层的两块，留给需要拆开用的场合
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import ready_adapters
from .adapters.base import AgentAdapter, ScanResult
from .models import Project, Session
from .store import Store


@dataclass
class RefreshResult:
    """一次 refresh 的全部产出。"""

    adapters: list[AgentAdapter] = field(default_factory=list)
    """本次真正参与扫描的接入。空列表表示一个都没就绪。"""

    scan: ScanResult = field(default_factory=ScanResult)
    removed: int = 0


async def discover(blocked: Sequence[Path] = ()) -> tuple[list[AgentAdapter], ScanResult]:
    """让所有就绪的接入各扫一遍，把结果合并。

    **``ready_adapters()`` 走线程池，这一步不能省。** 它内部会调 ``detect()``，
    而 Claude 的 ``detect()`` 要起子进程跑 ``claude --version``（超时 20s）。
    在 CLI 里这只是一次同步等待，在服务器里它会把整个事件循环卡住——
    所有 HTTP 请求和 WebSocket 推送一起停摆。接口本身是同步的（见
    :class:`~gateway.adapters.base.AgentAdapter`），包一层 ``to_thread`` 是最小代价。
    """
    adapters = await asyncio.to_thread(ready_adapters)
    if not adapters:
        return [], ScanResult()

    scans = await asyncio.gather(*(a.discover_projects(blocked) for a in adapters))
    return adapters, merge(list(scans))


async def refresh(store: Store, blocked: Sequence[Path] = ()) -> RefreshResult:
    """扫一遍并写库。这是"刷新项目列表"这个动作的完整定义。"""
    adapters, scan = await discover(blocked)
    # 一个接入都没有时**绝不能** prune：keep_ids 为空会把整个库清干净。
    # 有接入但确实一个项目都没扫到时 prune 是对的——项目本来就没了。
    removed = persist(store, scan, prune=bool(adapters))
    return RefreshResult(adapters=adapters, scan=scan, removed=removed)


def merge(scans: Sequence[ScanResult]) -> ScanResult:
    """把多个 Adapter 的发现结果合成一份。

    同一个目录被两个 Agent 用过时，会得到同一个 stable_id（项目 id 由**路径**派生，
    不由"哪个 Agent 发现的"派生），所以这里按 id 合并：
    会话取并集，最后活动时间取较新者。

    只有一个 Adapter 时这就是个恒等变换——但合并规则必须是明确的，
    否则接第二个 Agent 的那天才会发现"两个 Agent 看到同一个项目"没被处理。
    """
    merged = ScanResult()
    by_id: dict[str, Project] = {}
    buckets: dict[str, dict[str, Session]] = {}

    for scan in scans:
        merged.skipped_blocked.extend(scan.skipped_blocked)

        for project in scan.projects:
            existing = by_id.get(project.id)
            if existing is None:
                by_id[project.id] = project
                merged.projects.append(project)
            else:
                newest = max(existing.last_active_at or 0, project.last_active_at or 0)
                existing.last_active_at = newest or None
                existing.git_branch = existing.git_branch or project.git_branch

            bucket = buckets.setdefault(project.id, {})
            for session in scan.sessions.get(project.id, []):
                current = bucket.get(session.id)
                if current is None or (session.last_active_at or 0) > (
                    current.last_active_at or 0
                ):
                    bucket[session.id] = session

    for project_id, bucket in buckets.items():
        sessions = sorted(
            bucket.values(), key=lambda s: s.last_active_at or 0, reverse=True
        )
        merged.sessions[project_id] = sessions
        by_id[project_id].session_count = len(sessions)

    merged.projects.sort(key=lambda p: p.last_active_at or 0, reverse=True)
    return merged


def persist(store: Store, scan: ScanResult, *, prune: bool = True) -> int:
    """把扫描结果写进 SQLite，并删除扫描中已不存在的项目。

    只 upsert 不删除的话，被屏蔽或已消失的项目会永远留在库里，
    屏蔽清单就形同虚设。返回被删除的项目数。

    ``prune=False`` 用于"本次没有任何 Adapter 成功发现"的情况——
    此时 keep_ids 为空，照常删会把整个库清空。
    """
    for project in scan.projects:
        store.upsert_project(project)
    for sessions in scan.sessions.values():
        for session in sessions:
            store.upsert_session(session)

    if not prune:
        return 0
    return store.prune_missing_projects({p.id for p in scan.projects})
