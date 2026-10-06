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
from datetime import datetime
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


# ----------------------------------------------------------------------
# 会话历史（只读）
# ----------------------------------------------------------------------

#: 单条消息文本的截断长度。会话文件里塞得下整个文件的内容（Read/Write 的全文
#: 都在里面），原样转发会让"看看聊了什么"的请求背上几 MB。
HISTORY_TEXT_LIMIT = 2000


def _history_ts(record: dict) -> float | None:
    """ISO 时间戳转 epoch 秒。解析不了就放弃——它只影响显示，不值得为它冒进。"""
    raw = record.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _history_tool_detail(input_data: object) -> str | None:
    """从工具入参里挑一行最有信息量的摘要，规则与前端 ``ARG_FIELD`` 同思路。"""
    if not isinstance(input_data, dict) or not input_data:
        return None
    for key in ("file_path", "notebook_path", "command", "pattern", "path", "url", "query"):
        value = input_data.get(key)
        if value:
            return f"{key}={str(value)[:120]}"
    first = next(iter(input_data.values()))
    return str(first)[:120]


def read_history(root: Path, native_session_id: str, limit: int = 60) -> list[dict]:
    """读一个会话的最近聊天记录：用户消息、助手文本、工具调用摘要。

    按**文件名**找会话文件——文件名就是 session uuid，而目录名是转义过的路径、
    不可反解（理由见模块 docstring）。找不到返回空列表：文件可能已被用户清掉，
    这不是错误。

    **这里不做归属判断**：调用方（API 层）必须先拿库确认这个会话属于哪个项目，
    才允许走到文件这一步。
    """
    source: Path | None = None
    try:
        for project_dir in root.iterdir():
            candidate = project_dir / f"{native_session_id}.jsonl"
            if candidate.is_file():
                source = candidate
                break
    except OSError:
        return []
    if source is None:
        return []

    messages: list[dict] = []
    try:
        with source.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                # 主链之外的都跳过：isMeta 是命令回显/caveat 之类的元消息，
                # isSidechain 是子代理（Task 工具）的支线——混进聊天记录里全是噪音
                if record.get("isMeta") or record.get("isSidechain"):
                    continue
                kind = record.get("type")
                if kind not in ("user", "assistant"):
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                ts = _history_ts(record)
                content = message.get("content")
                blocks = (
                    content
                    if isinstance(content, list)
                    else [{"type": "text", "text": content}] if isinstance(content, str) else []
                )
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            messages.append({
                                "role": kind,
                                "text": text[:HISTORY_TEXT_LIMIT],
                                "ts": ts,
                            })
                    elif block_type == "tool_use" and kind == "assistant":
                        messages.append({
                            "role": kind,
                            "tool": block.get("name") or "?",
                            "detail": _history_tool_detail(block.get("input")),
                            "ts": ts,
                        })
                    # tool_result 不出现：它在 user 记录里，但没有 text block，天然被滤掉
    except OSError:
        return messages  # 读了一半坏了：把手头有的交出去，比整个失败有用
    return messages[-limit:] if limit > 0 else []
