"""PATH 搜索诊断——doctor 与各 Adapter locate 模块共用的机器。

背景：exe 发给别人后"明明装了 Agent 却扫不到"，最常见的原因是
**双击 exe 拿到的 PATH 和终端里的 PATH 不是同一份**（安装器写的是注册表
PATH 而 Explorer 会话还持着旧值；或 PATH 只进了 PowerShell $PROFILE）。
这一层把"doctor 到底搜了哪里、搜没搜到"变成可打印的事实，
代替"你再去终端试试"式的来回猜。

设计约束（CLAUDE.md 规则 2/10）：这里**不认识任何具体 Agent 的名字**——
候选命令名、已知位置、config 提示语都由各 Adapter 的 locate 模块传入。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

WHERE_TIMEOUT = 10.0


@dataclass(frozen=True)
class DiagRow:
    """一行诊断：label 是问题，value 是事实（可多行，由打印方缩进）。"""

    label: str
    value: str


def path_entries() -> list[str]:
    """本进程实际拿到的 PATH，按顺序、去引号、去空项。"""
    raw = os.environ.get("PATH") or ""
    entries = [entry.strip().strip('"') for entry in raw.split(os.pathsep)]
    return [entry for entry in entries if entry]


def _extensions() -> tuple[str, ...]:
    """Windows 上按 PATHEXT 展开候选扩展名，另补 .ps1——npm 的第三种垫片。

    .ps1 垫片 CreateProcess 跑不了，但它的存在解释了"npm 明明装了"：
    报告里让用户看见它，比只报告能跑的那两种更有诊断价值。
    """
    if os.name != "nt":
        return ()
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    exts = [ext.strip().lower() for ext in raw.split(";") if ext.strip()]
    for extra in (".exe", ".cmd", ".bat", ".ps1"):
        if extra not in exts:
            exts.append(extra)
    return tuple(exts)


def search_path(names: Sequence[str]) -> list[tuple[str, str]]:
    """按 PATH 顺序枚举「目录 × 命令名 × 候选扩展名」，返回既有文件的命中。

    和 ``shutil.which`` 的区别：which 停在第一个命中，这里**全部列出**——
    "PATH 里排在前面是个跑不起来的坏垫片"这类问题，只有全列才看得出来。
    """
    exts = _extensions()
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for directory in path_entries():
        for name in names:
            candidates = (name, *(name + ext for ext in exts)) if exts else (name,)
            for candidate in candidates:
                full = Path(directory) / candidate
                key = str(full).lower()
                if key in seen:
                    continue
                if full.is_file():
                    seen.add(key)
                    hits.append((name, str(full)))
    return hits


def where_probe(name: str) -> str:
    """用系统的 where/which 交叉验证一遍，输出原样给用户。

    价值在于对照：doctor 自己的 search_path 没找到、而 where.exe 找到了，
    说明两者看到的 PATH 不一致（或 where 的语义差异）——这本身就是线索。
    """
    if os.name == "nt":
        argv = ["where.exe", name]
    else:
        argv = ["/usr/bin/which", "-a", name]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            # 不指定 encoding：where.exe 输出的是系统 ANSI（中文 Windows 是
            # GBK），与各 Agent CLI 的 UTF-8 输出不同源，按 locale 解才不乱码
            timeout=WHERE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"（超过 {WHERE_TIMEOUT:g}s 没返回，已放弃）"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"（探针跑不起来：{exc}）"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return output or f"（exit {proc.returncode}，无输出）"


def path_rows(names: Sequence[str]) -> list[DiagRow]:
    """每个 locate.diagnose() 都要有的两行：PATH 全量搜索 + where 交叉探针。"""
    directories = path_entries()
    hits = search_path(names)
    if hits:
        by_name: dict[str, list[str]] = {}
        for name, path in hits:
            by_name.setdefault(name, []).append(path)
        lines = [f"共 {len(directories)} 个 PATH 目录，命中："]
        for name, paths in by_name.items():
            lines.append(f"  「{name}」")
            lines.extend(f"    {path}" for path in paths)
        search_value = "\n".join(lines)
    else:
        quoted = "、".join(f"「{name}」" for name in names)
        search_value = f"共 {len(directories)} 个 PATH 目录，{quoted} 全部没有命中"
    probe_lines = [f"「{name}」 {where_probe(name)}" for name in names]
    return [
        DiagRow("PATH 搜索", search_value),
        DiagRow("where 探针", "\n".join(probe_lines)),
    ]


def config_row(*, section: str, current: Sequence[str] | None, hint: str) -> DiagRow:
    """config 覆盖行：显示现值 + 怎么改。

    ``current=None`` 表示该 Adapter 不支持 config 覆盖（如 claude 只认
    环境变量），调用方就不该用它。
    """
    if current:
        argv = " ".join(current)
        value = f"当前已配置：[{section}] command = [\"{argv}\"]\n{hint}"
    else:
        value = f"当前未配置。{hint}"
    return DiagRow("config 覆盖", value)
