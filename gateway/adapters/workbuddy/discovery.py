"""从 CodeBuddy CLI 的本地存储发现历史项目与会话。

数据源：

    ~/.codebuddy/projects/<转义路径>/<session-uuid>.jsonl

布局与 Claude Code 同构，但**记录格式是 CodeBuddy 自己的**（实测 2.156.0）：

    {type: "message", role: "user"|"assistant", content: [...],
     timestamp: <毫秒>, sessionId, cwd, id, parentId}

    文本块是 ``input_text``（用户）/``output_text``（助手）——不是 Claude 的
    ``text``。工具块按 Anthropic block 类型（``tool_use``/``tool_result``），
    但这只在持久化侧推断自源码，登录后的真跑探针要再核对一遍。

与 Claude 一致的两条纪律：

  - 所有解析包在 try/except 里：单个文件坏了只跳过它
  - 真实路径从 JSONL 的 ``cwd`` 字段读，**不反解目录名**（转义规则不可靠）
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ...models import Project, Session
from ..base import BlockFilter, ScanResult, stable_id
from ..history import TEXT_LIMIT, tool_detail

#: 每个 JSONL 最多读多少行来抽取元信息（理由同 Claude 的 discovery）。
PEEK_LINES = 300

#: 会话标题的截断长度
TITLE_LIMIT = 120

#: 文本块的类型名——用户输入与助手输出各有一个名字，另一个是通用的 ``text``。
_TEXT_BLOCK_TYPES = ("input_text", "output_text", "text")


def _blocks(content: object) -> list[dict]:
    """content 字段 → block 列表。可能是列表、纯字符串或别的什么。"""
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str) and content.strip():
        return [{"type": "text", "text": content}]
    return []


def _timestamp(record: dict) -> float | None:
    """毫秒整数 → epoch 秒；ISO 字符串也认。解析不了就放弃，只影响显示。"""
    raw = record.get("timestamp")
    if isinstance(raw, (int, float)) and raw > 0:
        return raw / 1000
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None
    return None


def _peek_session_file(path: Path) -> dict:
    """只读文件开头若干行，抽出一个会话的元信息。"""
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
                if not isinstance(record, dict) or record.get("type") != "message":
                    continue
                if info["cwd"] is None:
                    info["cwd"] = record.get("cwd")
                if info["git_branch"] is None:
                    info["git_branch"] = record.get("gitBranch")
                if info["title"] is None and record.get("role") == "user":
                    for block in _blocks(record.get("content")):
                        if block.get("type") in _TEXT_BLOCK_TYPES:
                            text = (block.get("text") or "").strip()
                            if text:
                                info["title"] = text[:TITLE_LIMIT]
                                break
                if info["cwd"] and info["title"] is not None:
                    break
    except OSError:
        pass
    return info


def scan(root: Path, blocked: Sequence[Path] = (), agent_id: str = "") -> ScanResult:
    """扫描 CodeBuddy 的项目存储目录（结构同 Claude 的 scan，字段名不同）。"""
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
            if not info["cwd"] and not info["title"]:
                # 一条消息都解析不出来：坏文件、或只有快照行的空会话。
                # 它不是会话，别让它以"(无标题)"混进列表、还靠最新 mtime 排到最前。
                continue
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


def read_history(root: Path, native_session_id: str, limit: int = 60) -> list[dict]:
    """读一个会话的最近聊天记录：用户消息、助手文本、工具调用摘要。

    按**文件名**找会话文件（文件名就是 session uuid，目录名不可反解，
    理由见模块 docstring）。找不到返回空列表：文件可能已被用户清掉，
    这不是错误。

    **这里不做归属判断**：调用方（API 层）必须先拿库确认这个会话属于
    哪个项目，才允许走到文件这一步。
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
                if not isinstance(record, dict) or record.get("type") != "message":
                    continue
                kind = record.get("role")
                if kind not in ("user", "assistant"):
                    continue
                ts = _timestamp(record)
                for block in _blocks(record.get("content")):
                    btype = block.get("type")
                    if btype in _TEXT_BLOCK_TYPES:
                        text = (block.get("text") or "").strip()
                        if text:
                            messages.append({
                                "role": kind,
                                "text": text[:TEXT_LIMIT],
                                "ts": ts,
                            })
                    elif btype == "tool_use" and kind == "assistant":
                        messages.append({
                            "role": kind,
                            "tool": block.get("name") or "?",
                            "detail": tool_detail(block.get("input")),
                            "ts": ts,
                        })
                    # tool_result 不出现：它在 user 记录里，但没有文本块，天然被滤掉
    except OSError:
        return messages  # 读了一半坏了：把手头有的交出去，比整个失败有用
    return messages[-limit:] if limit > 0 else []
