"""定位 CodeBuddy CLI（WorkBuddy 内置的那个）与它的会话存储。

WorkBuddy 桌面应用自带一份 CodeBuddy Code CLI（腾讯，Claude Code 同型协议），
没有独立安装包时它就是唯一的来源：

    <WorkBuddy 安装目录>\\resources\\app.asar.unpacked\\cli\\bin\\codebuddy

这是一个 node 启动脚本（#!/usr/bin/env node，要求 Node ≥ 18.20.8），
Windows 上不能直接 exec，必须拼上 node 前缀。命令来源优先级：

    1. config.toml 的 ``[workbuddy] command = [...]``（argv 前缀，最灵活）
    2. PATH 上的 ``codebuddy`` / ``cbc``（单独 npm 安装的场合）
    3. 注册表卸载项反查 WorkBuddy 安装目录（安装时改过路径也能找到）
    4. NSIS 默认安装目录（兜底）

命令在**每次使用时**解析而不是导入时缓存（同 OpenCode 的理由：用户改了
配置或装了应用，重启网关就该生效）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: 允许用环境变量覆盖会话存储位置（CLI 自己认 CODEBUDDY_CONFIG_DIR 时用）。
PROJECTS_DIR_ENV = "REMOTE_AGENT_WORKBUDDY_PROJECTS_DIR"

VERSION_TIMEOUT = 60.0
"""node 冷启动 + 首次加载 11MB bundle，比普通二进制慢，超时给宽。"""


def find_command() -> tuple[str, ...] | None:
    """返回启动 CodeBuddy CLI 的 argv 前缀；找不到返回 None。"""
    from ...config import loaded_config  # 延迟导入：config → adapters.base，避免导入环

    config = loaded_config()
    if config is not None and config.workbuddy_command:
        return _resolve(config.workbuddy_command)

    for name in ("codebuddy", "cbc"):
        found = _which_cli(name)
        if found:
            return _resolve((found,))

    for launcher in _bundled_launchers():
        command = _as_executable(launcher)
        if command is not None:
            return command
    return None


def _which_cli(name: str) -> str | None:
    """PATH 查找，优先可被 CreateProcess 直接执行的形态。

    npm 全局包装了三种 shim：无扩展名 sh 脚本（git-bash 用）、``.cmd``、
    ``.ps1``。裸名查询在 PATHEXT 不全的环境会命中那个 sh 脚本，直接
    CreateProcess 就是 WinError 193（实测）；所以按 ``.cmd/.bat/.exe``
    显式找，extensionless 只作 POSIX 场合的兜底。
    """
    if os.name == "nt":
        for ext in (".cmd", ".bat", ".exe"):
            found = shutil.which(name + ext)
            if found:
                return found
    return shutil.which(name)


def _bundled_launchers() -> list[Path]:
    """WorkBuddy 内置 CLI 的候选路径：注册表卸载项 + NSIS 默认目录。"""
    candidates: list[Path] = []
    for install_dir in _install_dirs():
        candidates.append(
            install_dir / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"
        )
    return candidates


def _install_dirs() -> list[Path]:
    dirs: list[Path] = []
    if os.name == "nt":
        dirs.extend(_install_dirs_from_registry())
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Programs" / "WorkBuddy")
    return dirs


def _install_dirs_from_registry() -> list[Path]:
    """从卸载注册表项里挖 WorkBuddy 的安装目录。

    Electron/NSIS 的卸载项不一定有 InstallLocation，但 DisplayIcon 和
    UninstallString 都指向安装目录里的文件，取其父目录即可。
    """
    import winreg

    dirs: list[Path] = []
    hives = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subkey in hives:
        try:
            key = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        with key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                try:
                    name = winreg.EnumKey(key, index)
                    entry = winreg.OpenKey(key, name)
                except OSError:
                    continue
                # 字段读取必须都在 with 里：句柄一关 QueryValueEx 全部静默失败
                with entry:
                    try:
                        display = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                    except OSError:
                        continue
                    if "workbuddy" not in display.lower():
                        continue
                    for field in ("InstallLocation", "DisplayIcon", "UninstallString"):
                        try:
                            raw = str(winreg.QueryValueEx(entry, field)[0]).strip()
                        except OSError:
                            continue
                        # 带引号的路径（UninstallString 惯例）取引号内，
                        # 否则取第一个逗号前的段（DisplayIcon 的 ",0" 后缀）
                        first = raw[1:].split('"')[0] if raw.startswith('"') else raw.split(",")[0].strip()
                        if first.lower().endswith(".exe"):
                            dirs.append(Path(first).parent)
                        elif first:
                            dirs.append(Path(first))
                        if dirs and dirs[-1].name.lower() == "workbuddy":
                            break
    return list(dict.fromkeys(dirs))


def _as_executable(path: Path) -> tuple[str, ...] | None:
    """裸 node 启动脚本拼上 node 前缀；node 都没有就当没找到。"""
    if not path.is_file():
        return None
    node = shutil.which("node")
    if node is None:
        return None
    return (node, str(path))


def _resolve(command: tuple[str, ...]) -> tuple[str, ...]:
    """把裸命令名解析成绝对路径。

    同 OpenCode 的 _resolve：Windows 上 asyncio 对 ``.cmd`` 垫片无能为力，
    先解析出来才好决定要不要套 ``cmd /c``；也让 executable 字段展示真实路径。
    """
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
    return (first or None), (None if first else "codebuddy --version 没有输出")


def projects_dir() -> Path:
    """CodeBuddy 的项目/会话存储目录。

    ``~/.codebuddy/projects/<转义路径>/<session-uuid>.jsonl``

    与 Claude Code 的布局同构（目录名是转义过的路径、不可反解），
    真实路径从 JSONL 的 ``cwd`` 字段读。**这是未公开的内部格式**。
    """
    override = os.environ.get(PROJECTS_DIR_ENV)
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".codebuddy" / "projects"


def clean_env() -> dict[str, str]:
    """清掉"嵌套会话"标记变量后再传给子进程。

    网关自己可能就跑在某个 CLI 会话里（开发时就是），这些变量会让被拉起的
    子进程以为自己在替外层会话打工，走不同的代码路径。清掉后子进程表现为
    一个普通的独立会话。
    """
    env = dict(os.environ)
    for key in (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_SESSION_ID",
        "CODEBUDDY_SESSION_ID",
        "CODEBUDDY_CONVERSATION_REQUEST_ID",
        "CODEBUDDY_TOOL_CALL_ID",
    ):
        env.pop(key, None)
    return env
