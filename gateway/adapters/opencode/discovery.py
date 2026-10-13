"""定位 OpenCode 的启动命令。

和 Claude 不同，OpenCode 在这台机器上**可能没有可执行文件**——它可以从
源码仓库用 bun 直接跑（本机就是这样）。所以命令来源有两级：

    1. config.toml 的 ``[opencode] command = ["bun", "run", …]``（argv 前缀）
    2. PATH 上的 ``opencode``（正式安装的场合）

命令在**每次使用时**解析而不是导入时缓存：用户改了 config.toml、重启网关
就该生效，不该要求重启 Python 进程之外的东西。
"""

from __future__ import annotations

import shutil
import subprocess
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

VERSION_TIMEOUT = 30.0
"""bun 冷启动比普通二进制慢（实测数秒），超时给得比 claude 宽。"""


def known_dirs() -> list[KnownDir]:
    """PATH 之外的官方安装落点。

    ``~/.opencode/bin``：官方 curl/安装脚本的落点（源码
    ``installation/index.ts`` 以 ``.opencode/bin`` 识别自家安装）；
    其余为 npm/bun 全局目录。
    """
    return [
        KnownDir("官方安装脚本", Path.home() / ".opencode" / "bin"),
        *js_global_dirs(),
    ]


def find_command() -> tuple[str, ...] | None:
    """返回启动 OpenCode 的 argv 前缀；找不到返回 None。"""
    from ...config import loaded_config  # 延迟导入：config → adapters.base，避免导入环

    config = loaded_config()
    if config is not None and config.opencode_command:
        return _resolve(config.opencode_command)

    exe = shutil.which("opencode")
    if exe is not None:
        return _resolve((exe,))

    # 进程 PATH 没有：先查注册表 PATH 的差集（与安装方式无关），再查官方落点
    for entry in [*registry_path_dirs(), *known_dirs()]:
        hit = resolve_in_dir(entry.path, "opencode")
        if hit is not None:
            return _resolve((str(hit),))
    return None


def diagnose() -> list[DiagRow]:
    """doctor 用：opencode 的查找过程可打印化。"""
    from ...config import loaded_config  # 同 find_command 的理由：避免导入环

    config = loaded_config()
    current = config.opencode_command if config is not None else None
    return [
        *path_rows(("opencode",)),
        registry_path_row("opencode"),
        locations_row(known_dirs(), "opencode"),
        config_row(
            section="opencode",
            current=current,
            hint='没有正式安装时用 config.toml 指到源码入口：\n'
                 '  [opencode]\n'
                 '  command = ["bun", "run", "--cwd", "D:/…/packages/opencode", "src/index.ts"]',
        ),
    ]


def _resolve(command: tuple[str, ...]) -> tuple[str, ...]:
    """把裸命令名解析成绝对路径。

    两个原因：一是 asyncio 的 ``create_subprocess_exec`` 在 Windows 上对
    ``.cmd`` 垫片（npm 全局安装产物）无能为力，先解析出来才好判断要不要
    套一层 ``cmd /c``；二是让 ``executable`` 字段展示的是真实路径而不是 PATH 里的裸名。
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

    从源码跑的开发版会输出 "local" 而不是语义化版本号——它照样是有效版本
    信息，照常返回。这是唯一的同步子进程调用，只出现在检测阶段。
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
    return (first or None), (None if first else "opencode --version 没有输出")
