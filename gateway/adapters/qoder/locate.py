"""定位 Qoder CLI 与登录态。

安装形态（2026-10-09 实测）：npm 包 ``@qodercn-ai/qoderclicn``，PATH 上有
``qodercn``（npm dispatcher）与 ``qoderclicn``（bundle 直启）两个命令名；
dispatcher 内部还会 re-exec 到别的 node 目录，找 PATH 上的垫片即可。

CLI 与 Qoder 桌面端**凭证不互通**：桌面端登录不会让 CLI 可用，CLI 的
登录态独立存在 ``~/.qoder/.auth/``。所以"装了"和"能用"是两回事——
``status`` 一次调用同时给出版本号与登录态，detect 只跑这一个子进程。

命令在**每次使用时**解析而不是导入时缓存（同 Codex/WorkBuddy 的理由：
装了 CLI 或改了配置，重启网关就该生效）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

VERSION_TIMEOUT = 60.0
"""node 冷启动比 rust 慢，首次运行还可能做资源自检，超时给宽。"""


@dataclass(frozen=True)
class StatusInfo:
    """一次 ``status`` 调用的解析结果。"""

    version: str | None
    logged_in: bool
    failure: str | None = None
    """status 本身跑不起来时的原因；None 表示调用成功。"""


def find_command() -> tuple[str, ...] | None:
    """返回启动 Qoder CLI 的 argv 前缀；找不到返回 None。"""
    from ...config import loaded_config  # 延迟导入：config → adapters.base，避免导入环

    config = loaded_config()
    if config is not None and config.qoder_command:
        return _resolve(config.qoder_command)

    for name in ("qodercn", "qoderclicn"):
        found = _which_cli(name)
        if found:
            return _resolve((found,))
    return None


def _which_cli(name: str) -> str | None:
    """PATH 查找，优先可被 CreateProcess 直接执行的形态（理由同 WorkBuddy）。"""
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat"):
            found = shutil.which(name + ext)
            if found:
                return found
    return shutil.which(name)


def _resolve(command: tuple[str, ...]) -> tuple[str, ...]:
    """裸命令名解析成绝对路径；``.cmd/.bat`` 垫片套 ``cmd /c``（同 OpenCode）。"""
    head, *rest = command
    resolved = shutil.which(head)
    if resolved is None:
        return command
    if resolved.lower().endswith((".cmd", ".bat")):
        return ("cmd", "/c", resolved, *rest)
    return (resolved, *rest)


def run_status(command: tuple[str, ...]) -> StatusInfo:
    """跑 ``status``，解析版本号与登录态。

    输出形状（1.1.67 实测）：

        Version: 1.1.67
        Username: someone        # 未登录时这一行是 Account: Not logged in
        ...
        Auth Source: local

    登录判定看有没有 ``Username:`` 行——"Not logged in" 的具体措辞可能变，
    结构不会。
    """
    try:
        proc = subprocess.run(
            [*command, "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VERSION_TIMEOUT,
            cwd=Path.home(),
        )
    except subprocess.TimeoutExpired:
        return StatusInfo(None, False, f"status 超过 {VERSION_TIMEOUT:g}s 未返回")
    except (OSError, subprocess.SubprocessError) as exc:
        return StatusInfo(None, False, f"运行 status 失败：{exc}")

    output = (proc.stdout or "").strip()
    version = None
    logged_in = False
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Version:") and version is None:
            version = line.split(":", 1)[1].strip() or None
        elif line.startswith("Username:"):
            logged_in = True
    if not output:
        return StatusInfo(None, False, "qodercn status 没有输出")
    return StatusInfo(version, logged_in)


def clean_env() -> dict[str, str]:
    """传给子进程的环境。

    没有发现需要剔除的"嵌套会话"标记变量；QODER_* 配置与 ``~/.qoder``
    定位必须保留。网关本身由用户 shell 拉起，环境原样透传。
    """
    return dict(os.environ)
