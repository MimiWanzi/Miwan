"""从 Claude Code 的本地存储发现历史项目与会话。

数据源：

    ~/.claude/projects/<转义路径>/<session-uuid>.jsonl

**这是未公开的内部格式**，Anthropic 可能随时改。因此：

  - 所有解析都包在 try/except 里：单个文件坏了只跳过它，不影响整体扫描
  - 真实路径从 JSONL 里的 ``cwd`` 字段读，**不反解目录名**——转义规则不可靠
    （实测 ``D:\\RemoteAgent\\probes\\scratch`` 会被转义成
     ``D--RemoteAgent-probes-scratch``，但路径里本来就带连字符时无法可靠区分，
     所以干脆不猜。实测还发现磁盘上真有个目录叫 ``wd1@002d1@002dsh@...``，
     进一步证明目录名不可反解）

Codex 的对应模块扫 ``~/.codex/sessions``，OpenCode 则要调它的 HTTP API——
这就是为什么 :class:`~gateway.adapters.base.ScanResult` 定义在抽象层而不是这里。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from ...models import Project, Session
from ..base import BlockFilter, ScanResult, stable_id

#: 每个 JSONL 最多读多少行来抽取元信息。
#: 不读整个文件是刻意的——单个会话文件可以到几百 KB，全读会让扫描变慢且吃内存，
#: 而 cwd / gitBranch / 首条用户消息几乎总在文件开头。
PEEK_LINES = 300

#: 会话标题的截断长度
TITLE_LIMIT = 120


def _user_text(record: dict) -> str | None:
    """从一条 user 记录里取出用户真正输入的文字。

    事件流里的 ``user`` 消息有两种：用户输入的提示，以及工具返回结果的回填。
    后者是 ``tool_result`` block，这里靠只认 ``text`` block 天然过滤掉——
    否则会话标题会变成一大段文件内容。
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    return text
    return None


def _peek_session_file(path: Path) -> dict:
    """只读文件开头若干行，抽出一个会话的元信息。

    返回的字段可能为 None——调用方必须容忍解析不出东西的情况。
    """
    info: dict = {
        "cwd": None,
        "git_branch": None,
        "title": None,
        "native_session_id": path.stem,
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, raw in enumerate(handle):
                if index >= PEEK_LINES:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if info["cwd"] is None:
                    info["cwd"] = record.get("cwd")
                if info["git_branch"] is None:
                    info["git_branch"] = record.get("gitBranch")
                if info["title"] is None and record.get("type") == "user":
                    text = _user_text(record)
                    if text:
                        info["title"] = text[:TITLE_LIMIT]
                if info["cwd"] and info["title"] is not None:
                    break
    except OSError:
        pass
    return info


def scan(
    root: Path,
    blocked: Sequence[Path] = (),
    agent_id: str = "",
) -> ScanResult:
    """扫描 Claude Code 的项目存储目录。

    读不到 ``cwd`` 的目录会被跳过——宁可少列一个项目，也不猜一个可能错误的路径。
    """
    result = ScanResult()
    if not root.is_dir():
        return result

    is_blocked = BlockFilter(blocked)
    seen_paths: set[str] = set()

    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        session_files = sorted(project_dir.glob("*.jsonl"))
        if not session_files:
            continue

        project_path: str | None = None
        project_branch: str | None = None
        newest_mtime = 0.0
        pending: list[tuple[dict, float]] = []

        for session_file in session_files:
            try:
                mtime = session_file.stat().st_mtime
            except OSError:
                continue
            info = _peek_session_file(session_file)
            if project_path is None and info["cwd"]:
                project_path = info["cwd"]
            if project_branch is None and info["git_branch"]:
                project_branch = info["git_branch"]
            newest_mtime = max(newest_mtime, mtime)
            pending.append((info, mtime))

        if not project_path:
            continue
        if is_blocked(project_path):
            result.skipped_blocked.append(project_path)
            continue
        if project_path in seen_paths:
            continue
        seen_paths.add(project_path)

        project_id = stable_id(project_path)
        sessions = [
            Session(
                id=f"{project_id}:{info['native_session_id']}",
                project_id=project_id,
                agent_id=agent_id,
                native_session_id=info["native_session_id"],
                title=info["title"] or "(无标题)",
                git_branch=info["git_branch"],
                last_active_at=mtime,
            )
            for info, mtime in pending
        ]
        sessions.sort(key=lambda s: s.last_active_at or 0, reverse=True)

        result.projects.append(
            Project(
                id=project_id,
                name=Path(project_path).name or project_path,
                path=project_path,
                git_branch=project_branch,
                last_active_at=newest_mtime,
                session_count=len(sessions),
            )
        )
        result.sessions[project_id] = sessions

    result.projects.sort(key=lambda p: p.last_active_at or 0, reverse=True)
    return result
