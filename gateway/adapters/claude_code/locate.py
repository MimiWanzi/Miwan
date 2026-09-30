"""Claude Code 在这台机器上的位置与运行环境。

单独成文件的原因：这些是**唯一**硬编码了 Claude Code 安装细节的地方。
换成 Codex 时，它对应的 locate 模块里放的是 ``~/.codex`` 和 codex 可执行文件，
核心层不需要知道任何一个。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: 实测确认：本机的 claude CLI 是原生可执行文件（单文件 243MB）。
#: 直接调用它能绕开 .cmd / .ps1 shim——shim 会插一层 cmd.exe，
#: 而我们要用的是双工管道，中间多一层 shell 解释器是纯粹的干扰源。
EXE_CANDIDATES = (
    Path(
        r"E:\ProgramFiles\nodejs\node_global\node_modules"
        r"\@anthropic-ai\claude-code\bin\claude.exe"
    ),
)

#: 允许用环境变量覆盖，便于换机器或不走默认安装路径。
EXE_ENV = "REMOTE_AGENT_CLAUDE_EXE"
PROJECTS_DIR_ENV = "REMOTE_AGENT_CLAUDE_PROJECTS_DIR"


def find_exe() -> Path | None:
    """找 claude CLI。找不到返回 None——没装是正常情况，不是错误。"""
    override = os.environ.get(EXE_ENV)
    if override:
        candidate = Path(override)
        return candidate if candidate.exists() else None

    for candidate in EXE_CANDIDATES:
        if candidate.exists():
            return candidate

    found = shutil.which("claude")
    return Path(found) if found else None


def projects_dir() -> Path:
    """Claude Code 的项目/会话存储目录。

    ``~/.claude/projects/<转义路径>/<session-uuid>.jsonl``

    **这是未公开的内部格式**，Anthropic 可能随时改。发现逻辑因此整个包在
    try/except 里，且真实路径从 JSONL 的 ``cwd`` 字段读、不反解目录名。
    """
    override = os.environ.get(PROJECTS_DIR_ENV)
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".claude" / "projects"


def clean_env() -> dict[str, str]:
    """清掉"嵌套会话"标记变量后再传给子进程。

    Gateway 自己很可能就是从 Claude Code 里启动的（开发时就是），
    那些变量会让被拉起的子进程以为自己属于外层会话，从而走不同的代码路径。
    清掉后子进程表现为一个普通的独立会话——这才是手机上那个 Agent 该有的样子。

    实测：不清的话探针信号不干净。
    """
    env = dict(os.environ)
    for key in (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_SESSION_ID",
    ):
        env.pop(key, None)
    return env
