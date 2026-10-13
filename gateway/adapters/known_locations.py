"""npm 全局目录与各 CLI 官方安装落点——"装了却不在 PATH 上"的兜底。

为什么要有这一层（2026-10-13，第一个真实案例）：exe 发给朋友，双击启动
拿到的是 Explorer 会话的 PATH，终端里能跑的 CLI 在这里看不见。
规则：**只收有实证的落点**（本机实测 / 官方源码 / 官方文档），不拍脑袋
铺满——兜底清单里每一项都是启动时要做的磁盘探测，清单越长，
"到底为什么没找到"越说不清（doctor 的诊断价值也随之稀释）。

本模块不认识具体 Agent（规则 2/10）：调用方传入命令名，这里只负责
"目录 × 命令名 × 扩展名"的候选解析与"查过哪里"的可打印报告。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .diagnose import DiagRow, path_entries

NPM_PREFIX_TIMEOUT = 15.0
"""npm 首次运行可能偏慢（Windows 上 npm.cmd → node 一整条链），给宽。"""


def resolve_in_dir(directory: Path, name: str) -> Path | None:
    """在单个目录里找 ``name`` 的可执行形态，优先 CreateProcess 能直接跑的。

    Windows 上**不匹配裸名**：npm 全局目录里裸名文件是没有扩展名的 sh
    垫片（给 git-bash 用的），CreateProcess 跑不了——实测本机 npm prefix
    里 ``claude``/``claude.cmd``/``claude.ps1`` 三件套并存，裸名优先会把
    sh 垫片选走。
    """
    if os.name != "nt":
        full = directory / name
        return full if full.is_file() else None
    for ext in (".exe", ".cmd", ".bat"):
        full = directory / (name + ext)
        if full.is_file():
            return full
    return None


@dataclass(frozen=True)
class KnownDir:
    """一个兜底候选目录。label 用于诊断报告——不同来源没命中，下一步不同。"""

    label: str
    path: Path


def js_global_dirs() -> list[KnownDir]:
    """JS 系 CLI 的全局包落点，按可信度排序。

    1. ``npm config get prefix``——最准：自定义 prefix（国内常见的
       ``node_global``）也能报对；代价是一个子进程，**只在 PATH 没找到时**
       才值得付。npm 本身也不在 PATH 时静默跳过，退回静态猜测。
    2. ``%APPDATA%\\npm``——npm 在 Windows 的默认全局 bin。
    3. ``%NVM_SYMLINK%``——nvm-windows 当前激活版本的目录（默认全局前缀）。
    4. ``~/.bun/bin``——bun 全局包落点（opencode 常经 bun 安装）。
    """
    dirs: list[KnownDir] = []

    npm = shutil.which("npm")
    if npm is not None:
        argv = ("cmd", "/c", npm, "config", "get", "prefix") \
            if npm.lower().endswith((".cmd", ".bat")) else (npm, "config", "get", "prefix")
        try:
            # 不指定 encoding：npm 输出的是系统 ANSI（路径可能含中文用户名），
            # 与各 Agent CLI 的 UTF-8 输出不同源
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=NPM_PREFIX_TIMEOUT,
                cwd=Path.home(),
            )
            if proc.returncode == 0:
                prefix = (proc.stdout or "").strip()
                if prefix:
                    dirs.append(KnownDir("npm 全局 prefix", Path(prefix)))
        except (OSError, subprocess.SubprocessError):
            pass  # npm 跑不起来不算失败——后面还有静态猜测

    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(KnownDir("npm 默认全局", Path(appdata) / "npm"))
    symlink = os.environ.get("NVM_SYMLINK")
    if symlink:
        dirs.append(KnownDir("nvm 当前版本", Path(symlink)))
    dirs.append(KnownDir("bun 全局", Path.home() / ".bun" / "bin"))

    return list(dict.fromkeys(dirs))


def locations_row(entries: Sequence[KnownDir], *names: str) -> DiagRow:
    """一行"PATH 之外还查了哪些位置、结果如何"——doctor 与排障共用的事实行。"""
    lines: list[str] = []
    for entry in entries:
        hits = [
            f"「{name}」{hit.name}"
            for name in names
            if (hit := resolve_in_dir(entry.path, name)) is not None
        ]
        state = "、".join(hits) if hits else "没有命中"
        lines.append(f"{entry.path}（{entry.label}）\n    → {state}")
    return DiagRow("已知位置（PATH 之外）", "\n".join(lines))


def registry_path_dirs() -> list[KnownDir]:
    """注册表 PATH 里**本进程没拿到**的目录——"双击 exe 拿旧 PATH"的解药。

    原理：几乎所有安装方式（npm、Claude 原生安装器、opencode 安装脚本、
    scoop、volta、choco、winget……）最终都是"装到某个目录 + 把目录写进
    PATH"，而 PATH 的权威现值在注册表（HKLM 系统 + HKCU 用户），本进程
    继承到的只是一份可能过期的副本——Explorer 在安装之后不刷新，双击
    启动的 exe 就看不见新装的 CLI（2026-10-13 朋友机器案例的根源）。

    所以这里读注册表、按"不在进程 PATH 里"取差集：差集为空说明进程
    PATH 是新鲜的；非空的每一项都是"安装器写了、这个进程却看不见"的
    目录。与安装方式无关——不需要为每种安装器维护一个候选位置。

    只在 Windows 有效，其它平台返回空。
    """
    if os.name != "nt":
        return []
    import winreg

    seen = {entry.lower() for entry in path_entries()}
    dirs: list[KnownDir] = []
    sources = (
        ("系统 PATH", winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ("用户 PATH", winreg.HKEY_CURRENT_USER, r"Environment"),
    )
    for label, hive, subkey in sources:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                raw, value_type = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue  # 没这个值/读不了都不是错误——换下一个来源
        value = (winreg.ExpandEnvironmentStrings(raw)
                 if value_type == winreg.REG_EXPAND_SZ else raw)
        for entry in value.split(";"):
            entry = entry.strip().strip('"')
            if not entry:
                continue
            if entry.lower() in seen:
                continue
            seen.add(entry.lower())
            dirs.append(KnownDir(label, Path(entry)))
    return dirs


def registry_path_row(*names: str) -> DiagRow:
    """doctor 行：注册表 PATH 与进程 PATH 的差异 + 差异目录里的检查结果。"""
    delta = registry_path_dirs()
    if not delta:
        return DiagRow("注册表 PATH", "与进程 PATH 一致——不存在「装了但进程拿旧值」问题")
    lines: list[str] = []
    for entry in delta:
        hits = [
            f"「{name}」{hit.name}"
            for name in names
            if (hit := resolve_in_dir(entry.path, name)) is not None
        ]
        state = "、".join(hits) if hits else "没有命中"
        lines.append(f"{entry.path}（{entry.label}）\n    → {state}")
    return DiagRow("注册表 PATH（进程没拿到的部分）", "\n".join(lines))
