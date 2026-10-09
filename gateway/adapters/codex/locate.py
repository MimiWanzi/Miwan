"""定位 Codex CLI 与它的会话存储。

本机的 Codex 来自 Codex Desktop 应用，**不在 PATH 上**，二进制在版本化的
hash 目录里（2026-10-09 实测 ``%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe``，
当时同时存在两个 hash 目录）——应用升级会换目录，路径不能硬编码，只能扫。
命令来源优先级：

    1. config.toml 的 ``[codex] command = [...]``（argv 前缀；单装了 CLI 的机器
       或想钉死某个版本时用）
    2. PATH 上的 ``codex``（npm/独立安装的场合，``.cmd`` 垫片要显式按扩展名找）
    3. Desktop 应用内置的那份：扫 ``bin/*/codex.exe`` 取修改时间最新的

模型后端由 ``~/.codex/config.toml`` 自己决定（本机走 Desktop 管的本地代理
``127.0.0.1:15721``）——网关不碰这份配置，代理不在线时 exec 会连接失败，
错误如实落到 RunOutcome。命令在**每次使用时**解析而不是导入时缓存（同
OpenCode/WorkBuddy 的理由：装了应用或改了配置，重启网关就该生效）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: 允许用环境变量覆盖会话存储位置（探针用假 rollout 文件喂 discovery）。
SESSIONS_DIR_ENV = "REMOTE_AGENT_CODEX_SESSIONS_DIR"

VERSION_TIMEOUT = 60.0
"""rust 二进制冷启动比 node 快，但 Desktop 版首次运行可能做自检，超时仍给宽。"""


def find_command() -> tuple[str, ...] | None:
    """返回启动 Codex CLI 的 argv 前缀；找不到返回 None。"""
    from ...config import loaded_config  # 延迟导入：config → adapters.base，避免导入环

    config = loaded_config()
    if config is not None and config.codex_command:
        return _resolve(config.codex_command)

    found = _which_cli("codex")
    if found:
        return _resolve((found,))

    bundled = _bundled_binary()
    if bundled is not None:
        return (str(bundled),)
    return None


def _which_cli(name: str) -> str | None:
    """PATH 查找，优先可被 CreateProcess 直接执行的形态（理由同 WorkBuddy）。"""
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat"):
            found = shutil.which(name + ext)
            if found:
                return found
    return shutil.which(name)


def _bundled_binary() -> Path | None:
    """Desktop 应用内置的 codex.exe：``bin/<hash>/codex.exe`` 取 mtime 最新。

    hash 目录名随版本变，旧的升级后可能残留——最新 mtime 是"应用当前在用
    那份"的最好近似。找不到或目录为空返回 None。
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    bin_root = Path(local) / "OpenAI" / "Codex" / "bin"
    if not bin_root.is_dir():
        return None
    candidates = [
        path for path in bin_root.glob("*/codex.exe") if path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _resolve(command: tuple[str, ...]) -> tuple[str, ...]:
    """裸命令名解析成绝对路径；``.cmd/.bat`` 垫片套 ``cmd /c``（同 OpenCode）。"""
    head, *rest = command
    resolved = shutil.which(head)
    if resolved is None:
        return command
    if resolved.lower().endswith((".cmd", ".bat")):
        return ("cmd", "/c", resolved, *rest)
    return (resolved, *rest)


def run_version(command: tuple[str, ...]) -> tuple[str | None, str | None]:
    """跑 ``--version``，返回 ``(版本号, 失败原因)``。

    这是唯一的同步子进程调用，只出现在检测阶段，不在任务路径上。
    输出形如 ``codex-cli 0.162.0-alpha.2``，整行当作版本号展示。
    """
    try:
        proc = subprocess.run(
            [*command, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VERSION_TIMEOUT,
            cwd=Path.home(),
        )
    except subprocess.TimeoutExpired:
        return None, f"--version 超过 {VERSION_TIMEOUT:g}s 未返回"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"运行 --version 失败：{exc}"

    output = (proc.stdout or "").strip().splitlines()
    first = output[0].strip() if output else ""
    return (first or None), (None if first else "codex --version 没有输出")


def sessions_dir() -> Path:
    """Codex 的会话存储目录。

    ``~/.codex/sessions/<年>/<月>/<日>/rollout-<时间戳>-<uuid>.jsonl``

    **这是未公开的内部格式**（0.162.0-alpha.2 实测）：文件名同时含启动
    时间和会话 uuid，resume 的续轮**追加进同一文件**而不是分叉新文件
    （同日多文件实测确认），真实 cwd 从首行 session_meta 记录读。
    """
    override = os.environ.get(SESSIONS_DIR_ENV)
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".codex" / "sessions"


def clean_env() -> dict[str, str]:
    """传给子进程的环境。

    与 WorkBuddy/Claude 不同，目前**没有发现** Codex 的"嵌套会话"标记
    变量；``CODEX_HOME`` 之类的变量必须保留（CLI 靠它定位配置和认证）。
    模型后端（本地代理）由 ``~/.codex/config.toml`` 决定，环境不掺和。
    """
    return dict(os.environ)
