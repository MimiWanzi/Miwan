"""扫描 ``~/.codex/sessions`` 还原项目与会话；读会话正文。

布局（0.162.0-alpha.2 实测，未公开内部格式）::

    ~/.codex/sessions/<年>/<月>/<日>/rollout-<启动时间>-<会话uuid>.jsonl

每行一个 JSON 记录，带顶层 ``timestamp``（ISO）。要认识的类型：

  - ``session_meta``：首行，``payload.session_id`` / ``payload.cwd`` /
    ``payload.timestamp``——真实路径从这读，**目录名不可反解**（同 Claude）
  - ``response_item`` + ``payload.type == "message"``：对话正文，
    ``content[].type`` 是 ``input_text`` / ``output_text``（不是 Claude 的
    ``text`` 块）；user 消息里有 ``<environment_context>`` 之类的包装文本，
    既不能当标题也不能进历史
  - ``function_call`` / ``custom_tool_call``：工具调用（apply_patch 走后者），
    ``arguments`` 是 **JSON 字符串**不是对象
  - ``reasoning``：纯噪音，跳过（同 Claude 的 thinking_tokens 惯例）

resume 的续轮**追加进同一文件**（同日实测确认），所以一个会话一个文件；
防御性处理：真出现同 uuid 多文件时取 mtime 最新的那份。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from collections.abc import Sequence

from ...models import Project, Session, stable_id
from ..base import BlockFilter, ScanResult
from ..history import TEXT_LIMIT, tool_detail

#: 判断 cwd/title 只需要文件开头这一段；session_meta 和首条用户消息都在前几行。
PEEK_LINES = 300


def _parse_ts(raw: object) -> float | None:
    """ISO 时间戳 → epoch 秒；解析不了返回 None。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _in_temp(directory: str) -> bool:
    """目录是否在系统临时目录里——探针的一次性会话都在那，不还原成项目。"""
    try:
        Path(directory).relative_to(Path(__import__("tempfile").gettempdir()))
        return True
    except ValueError:
        return False


def _peek(path: Path) -> dict:
    """只读文件前 PEEK_LINES 行，提取会话 id / cwd / 标题 / 时间。

    解析不了的文件返回的 dict 各字段可能缺失——调用方按"缺 cwd 或缺标题
    即噪音"处理（同 WorkBuddy 的 _peek_session_file）。
    """
    info: dict = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for _ in range(PEEK_LINES):
            line = handle.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            rtype = record.get("type")
            payload = record.get("payload")
            if rtype == "session_meta" and isinstance(payload, dict):
                if not info.get("session_id"):
                    info["session_id"] = payload.get("session_id") or payload.get("id")
                if not info.get("cwd"):
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        info["cwd"] = cwd
                ts = _parse_ts(payload.get("timestamp"))
                if ts and not info.get("ts"):
                    info["ts"] = ts

            if rtype == "response_item" and isinstance(payload, dict) \
                    and payload.get("type") == "message" \
                    and payload.get("role") == "user" \
                    and not info.get("title"):
                for block in payload.get("content") or []:
                    text = ""
                    if isinstance(block, dict) and block.get("type") == "input_text":
                        text = str(block.get("text") or "").strip()
                    # "<" 开头的是 environment_context / user_instructions 包装，
                    # 不是用户敲的标题
                    if text and not text.startswith("<"):
                        info["title"] = text[:120]
                        break
    return info


def _rollout_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(root.glob("**/rollout-*.jsonl"))


def scan(root: Path, blocked: Sequence[Path], agent_id: str) -> ScanResult:
    """扫 rollout 文件 → ScanResult。每文件 try/except，坏文件只影响自己。"""
    result = ScanResult()
    is_blocked = BlockFilter(blocked)

    groups: dict[str, dict] = {}
    for path in _rollout_files(root):
        try:
            info = _peek(path)
        except OSError:
            continue
        cwd = info.get("cwd")
        if not cwd or _in_temp(cwd):
            continue
        if is_blocked(cwd):
            result.skipped_blocked.append(cwd)
            continue
        session_id = info.get("session_id") or path.stem.rsplit("-", 1)[-1]
        mtime = path.stat().st_mtime
        # 同 uuid 多文件（防御性，0.162 实测不会发生）：取 mtime 最新的
        known = groups.get(session_id)
        if known is not None and known["mtime"] >= mtime:
            continue
        groups[session_id] = {
            "path": path,
            "cwd": cwd,
            "title": info.get("title") or "(无标题)",
            "ts": info.get("ts"),
            "mtime": mtime,
        }

    projects: dict[str, Project] = {}
    for session_id, g in groups.items():
        cwd = g["cwd"]
        project_id = stable_id(cwd)
        session = Session(
            id=f"{project_id}:{session_id}",
            project_id=project_id,
            agent_id=agent_id,
            native_session_id=session_id,
            title=g["title"],
            last_active_at=g["mtime"],
        )
        project = projects.get(project_id)
        if project is None:
            projects[project_id] = Project(
                id=project_id,
                name=Path(cwd).name or cwd,
                path=cwd,
                git_branch=None,
                last_active_at=g["mtime"],
                session_count=1,
            )
            result.sessions[project_id] = [session]
        else:
            project.session_count += 1
            project.last_active_at = max(project.last_active_at or 0.0, g["mtime"])
            result.sessions[project_id].append(session)

    result.projects.extend(projects.values())
    result.projects.sort(key=lambda p: p.last_active_at or 0, reverse=True)
    return result


def read_history(root: Path, native_session_id: str, limit: int = 60) -> list[dict] | None:
    """按会话 uuid 找 rollout 文件，还原消息条目（adapters/history.py 形状）。

    返回 None 表示会话文件不存在（与"不支持"同待遇）；解析失败返回已读到的
    部分——历史页显示半截比报错更接近事实。
    """
    matches = [
        path for path in _rollout_files(root)
        if native_session_id in path.stem
    ]
    if not matches:
        return None
    path = max(matches, key=lambda p: p.stat().st_mtime)

    entries: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                ts = _parse_ts(record.get("timestamp"))
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                ptype = payload.get("type")
                if ptype == "message":
                    role = payload.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    for block in payload.get("content") or []:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        text = str(block.get("text") or "").strip() if btype in (
                            "input_text", "output_text", "text",
                        ) else ""
                        # "<" 开头的 user 文本是环境包装，不是对话正文
                        if not text or (role == "user" and text.startswith("<")):
                            continue
                        entries.append({
                            "role": role,
                            "text": text[:TEXT_LIMIT],
                            "ts": ts,
                        })
                elif ptype == "function_call":
                    arguments = payload.get("arguments")
                    parsed: object = None
                    if isinstance(arguments, str) and arguments.strip():
                        try:
                            parsed = json.loads(arguments)
                        except json.JSONDecodeError:
                            parsed = None
                    entries.append({
                        "role": "assistant",
                        "tool": str(payload.get("name") or "?"),
                        "detail": tool_detail(parsed) or (
                            str(arguments)[:120] if isinstance(arguments, str) and arguments else None
                        ),
                        "ts": ts,
                    })
                elif ptype == "custom_tool_call":
                    # apply_patch 之类：input 是补丁正文，取首行当摘要
                    raw = payload.get("input")
                    first_line = ""
                    if isinstance(raw, str) and raw.strip():
                        first_line = raw.strip().splitlines()[0][:120]
                    entries.append({
                        "role": "assistant",
                        "tool": str(payload.get("name") or "?"),
                        "detail": first_line or None,
                        "ts": ts,
                    })
                # reasoning / function_call_output / turn_context / event_msg：跳过
    except OSError:
        return entries or None

    return entries[-limit:] if limit > 0 else []
