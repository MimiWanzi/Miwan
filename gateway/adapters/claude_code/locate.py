"""Claude Code 在这台机器上的位置与运行环境。

单独成文件的原因：这些是**唯一**硬编码了 Claude Code 安装细节的地方。
换成 Codex 时，它对应的 locate 模块里放的是 ``~/.codex`` 和 codex 可执行文件，
核心层不需要知道任何一个。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..diagnose import DiagRow, config_row, path_rows
from ..known_locations import (
    KnownDir,
    js_global_dirs,
    locations_row,
    registry_path_dirs,
    registry_path_row,
    resolve_in_dir,
)

#: 允许用环境变量覆盖，便于换机器或不走默认安装路径。
EXE_ENV = "REMOTE_AGENT_CLAUDE_EXE"
PROJECTS_DIR_ENV = "REMOTE_AGENT_CLAUDE_PROJECTS_DIR"


def known_dirs() -> list[KnownDir]:
    """PATH 之外的官方安装落点，按实证程度排序。

    - ``~/.local/bin``：原生安装器的落点（安装器会写 PATH，但双击 exe 的
      会话可能持着旧值——位置兜底让这层差异不再致命）
    - ``~/.claude/local``：旧版 migrate-installer 的落点，存量机器还有
    - npm 系全局目录（见 :mod:`adapters.known_locations`）
    """
    home = Path.home()
    return [
        KnownDir("原生安装器", home / ".local" / "bin"),
        KnownDir("旧版迁移安装", home / ".claude" / "local"),
        *js_global_dirs(),
    ]


def find_exe() -> Path | None:
    """找 claude CLI。找不到返回 None——没装是正常情况，不是错误。

    先 ``shutil.which``；PATH 没有（双击 exe 拿到的 PATH 可能是 Explorer
    会话的旧值）再逐个试官方安装落点。安装位置随安装方式变，所以兜底
    只收有实证的位置，不写死大概率失效的候选路径。
    which 找到的若是 shim（.cmd/.ps1），上面 REMOTE_AGENT_CLAUDE_EXE
    可以指到真正的原生可执行文件——双工管道不希望中间多一层 shell。
    """
    override = os.environ.get(EXE_ENV)
    if override:
        candidate = Path(override)
        return candidate if candidate.exists() else None

    found = shutil.which("claude")
    if found:
        return Path(found)
    # 进程 PATH 没有：先查注册表 PATH 的差集（安装器写了但本进程没拿到的，
    # 与安装方式无关），再查官方落点（根本不进 PATH 的结构性场景）
    for entry in [*registry_path_dirs(), *known_dirs()]:
        hit = resolve_in_dir(entry.path, "claude")
        if hit is not None:
            return hit
    return None


def diagnose() -> list[DiagRow]:
    """doctor 用：claude 的查找过程可打印化。

    查找逻辑只有 PATH 一处（find_exe 刻意如此，理由见它的 docstring），
    所以诊断就是两件事：证明"这个进程的 PATH 里确实没有 claude"，
    以及指出仅有的两条出路——环境变量覆盖和装 CLI 本身。
    """
    override = os.environ.get(EXE_ENV)
    override_value = (
        f"已设置：{override}（{'文件存在' if Path(override).exists() else '文件不存在，会被当作没找到'}）"
        if override
        else "未设置"
    )
    return [
        *path_rows(("claude",)),
        registry_path_row("claude"),
        locations_row(known_dirs(), "claude"),
        DiagRow(f"环境变量 {EXE_ENV}", override_value),
        config_row(
            section="claude_code",
            current=None,
            hint="claude 不走 config.toml 命令覆盖；安装 CLI（npm i -g @anthropic-ai/claude-code "
                 "或原生安装器）并确保它进 PATH 后重启网关即可。",
        ),
    ]


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
