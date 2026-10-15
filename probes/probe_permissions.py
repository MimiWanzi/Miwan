#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：第 6 层的权限档位到底怎么写才算数。

要回答三个不猜的问题：

  1. **不加任何权限参数时，默认是什么行为？**
     这是基线。不加基线就分不清"我加的 flag 生效了"和"本来就这样"。
     本机跑在用户的 Claude Code 配置下，``~/.claude/settings.json`` 里的
     allow 规则会被子进程继承——所以基线很可能是"几乎都能干"，
     而那并不意味着我们的档位写对了。

  2. **哪组 flag 能让"读 + 写 + 执行命令"在无人应答时全部通过**，
     且**不碰 ``bypassPermissions`` / ``--dangerously-skip-permissions``。
     用户要的是"网关作为 host 决定放行"（可审计、产生事件），
     不是"关掉权限系统"——这两者留下的东西完全不同。

  3. **``--disallowedTools`` 的 glob 模式能不能拦住一条 PowerShell 命令？**
     绊线清单要靠它落地。help 的例子是 ``Bash(git *)``，本机没有 Bash。

判据是**可观测的副作用**，不是模型自己的说法：让它在工作目录里建两个文件
——一个用 Write、一个用 PowerShell——然后看文件在不在。
模型报"两件都成功了"而文件不在，是发生过的。

第一次跑这个探针踩了两个坑，都记在下面，因为同样的坑还会有人踩：
  - 没给 subprocess 指定 ``encoding="utf-8"``：Windows 上按 GBK 解码，
    中文全成乱码，而且失败时"什么都没有"看起来像"没有输出"
  - 丢掉了 exit code 和 stderr：B/C/E 三组全是空输出，当时无从判断
    是"被拦住了"还是"进程直接报错退出"，差点得出完全相反的结论
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "probes" / "out"

#: 所有组合**共用这一个工作目录**，不是每个组合一个。
#:
#: 第一版是按组合名分的目录（perm/F、perm/C……），结果这些目录被
#: `~/.claude/projects/` 记成了项目，于是手机的项目列表里多出 `F`、`C`、`E`、`tripwire`
#: 四个垃圾条目——跑一次探针污染一次用户的项目列表。
#:
#: 根因是 :class:`~gateway.adapters.base.BlockFilter` 只做**精确匹配**
#: （这是对的：前缀匹配会误杀 `C:\Users\<用户名>` 下的真实项目）。
#: 所以探针不能指望"在屏蔽目录的子目录里跑"，得直接用被屏蔽的那个目录本身。
#: `probes/scratch` 就在 ``default_blocked_paths()`` 里。
SCRATCH = ROOT / "probes" / "scratch"

_claude = shutil.which("claude")
if _claude is None:
    raise SystemExit("PATH 上找不到 claude CLI：装好 Claude Code 再跑本探针")
CLAUDE = Path(_claude)

PROMPT = (
    "做两件事，两件都要做：\n"
    "1. 用 Write 工具创建 file_write.txt，内容写 ok\n"
    "2. 用 PowerShell 工具运行一条命令，把 ran 写入 cmd_run.txt\n"
    "做完后用一句话说明哪件事成功了、哪件事没成功。"
)


@dataclass
class Combo:
    name: str
    args: list[str] = field(default_factory=list)
    note: str = ""


#: 只留还没答案的两组。F/A/D 已经跑过了，结论记在这儿就不再花钱重跑：
#:
#:   **F 什么都不加 → 写和跑命令全过。**
#:   这是本次探针最重要的一条，而且它是个警告：本机"默认就能干一切"，
#:   所以 A（acceptEdits）和 D（auto）跑出来的 ✓ ✓ **什么也证明不了**——
#:   它们和不加参数的基线一模一样，说明生效的可能是用户自己的
#:   ``~/.claude/settings.json``，而不是我们写的那两个 flag。
#:   也就是说：**基线组不是对照组，它是污染源。** 要断言"某个档位有效"，
#:   必须先有办法把用户配置隔离掉，否则测的是用户机器，不是我们的代码。
COMBOS = [
    Combo("C 只用 allowedTools", ["--allowedTools", "Read", "Grep", "Glob", "Write", "Edit", "PowerShell"],
          "完全不指定 permission-mode，能不能靠自己放行"),
    Combo("E C + 禁止递归删除",
          ["--allowedTools", "Read", "Grep", "Glob", "Write", "Edit", "PowerShell",
           "--disallowedTools", "PowerShell(Remove-Item*-Recurse*)"],
          "绊线能不能用 glob 表达"),
]


def invoke(args: list[str], workdir: Path, prompt: str) -> dict:
    """跑一次 claude，把**进程层面**的信息也带回来。

    exit code 和 stderr 不是调试用的附加品：没有它们，一次空输出无法区分
    "被权限系统拦下了"和"参数写错了直接退出"——而这两件事的结论完全相反。

    **prompt 必须紧跟 ``-p``**：``--allowedTools`` 的签名是 ``<tools...>``，
    是个可变参数列表，会一路吃后面的东西。第一版把 prompt 放在最后，
    结果它被 ``--allowedTools`` 吞掉，claude 报
    "Input must be provided either through stdin or as a prompt argument"
    然后退出 1——看起来像"权限被拒"，实际是参数没送到。
    """
    proc = subprocess.run(
        [str(CLAUDE), "-p", prompt, "--output-format", "json",
         "--session-id", str(uuid.uuid4()), *args],
        cwd=str(workdir), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    payload = None
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        pass
    return {
        "code": proc.returncode,
        "payload": payload,
        "stderr": (proc.stderr or "").strip().replace("\n", " ")[-300:],
        "stdout": (proc.stdout or "").strip().replace("\n", " ")[-300:],
    }


def probe_combo(combo: Combo, workdir: Path) -> dict:
    for name in ("file_write.txt", "cmd_run.txt"):
        (workdir / name).unlink(missing_ok=True)
    r = invoke(combo.args, workdir, PROMPT)
    payload = r["payload"] or {}
    return {
        "wrote": (workdir / "file_write.txt").is_file(),
        "ran": (workdir / "cmd_run.txt").is_file(),
        "denials": [str(d.get("tool_name")) for d in payload.get("permission_denials") or []],
        "said": str(payload.get("result") or "")[:150],
        "code": r["code"],
        "stderr": r["stderr"],
        "stdout": "" if payload else r["stdout"],
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if not CLAUDE.is_file():
        print(f"找不到 claude：{CLAUDE}")
        return 2

    results: list[tuple[Combo, dict]] = []
    workdir = SCRATCH
    workdir.mkdir(parents=True, exist_ok=True)
    for combo in COMBOS:
        # 每轮开始前清掉上一轮的产物，这样"文件在不在"只反映本轮
        print(f"跑 {combo.name} …", flush=True)
        try:
            results.append((combo, probe_combo(combo, workdir)))
        except subprocess.TimeoutExpired:
            results.append((combo, {"wrote": False, "ran": False, "denials": ["<超时>"],
                                    "said": "", "code": -1, "stderr": "", "stdout": ""}))

    print("\n" + "=" * 92)
    print(f"{'组合':<24} {'建文件':<7} {'跑命令':<7} {'exit':<5} {'被拒':<14} 备注")
    print("-" * 92)
    for combo, r in results:
        mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
        note = r["stderr"] or ("（无 JSON 输出）" if r["stdout"] else "")
        print(
            f"{combo.name:<24} {mark(r['wrote']):<7} {mark(r['ran']):<7} "
            f"{r['code']:<5} {('、'.join(r['denials']) or '—'):<14} {note[:40]}"
        )
    print("=" * 92)
    for combo, r in results:
        print(f"\n[{combo.name}] {combo.note}")
        print(f"  模型说：{r['said'] or '（无输出）'}")

    # ---- 第二阶段：disallow 的 glob 到底拦不拦得住 ----
    print("\n" + "=" * 92)
    print('绊线测试：--disallowedTools "PowerShell(Remove-Item*-Recurse*)"')
    print("-" * 92)
    tripwire: dict = {}
    e_combo = next((c for c, _ in results if c.name.startswith("E")), None)
    if e_combo is not None:
        # 同样用被屏蔽的那个目录做 cwd，别再往下开子目录——
        # 子目录会被 `~/.claude/projects/` 记成一个新项目，然后出现在手机上
        workdir = SCRATCH
        shutil.rmtree(workdir / "victim", ignore_errors=True)
        (workdir / "victim").mkdir(parents=True)
        (workdir / "victim" / "keep.txt").write_text("重要内容", encoding="utf-8")
        print("跑 递归删除 victim/ …", flush=True)
        try:
            r = invoke(
                e_combo.args, workdir,
                "用 PowerShell 工具执行 `Remove-Item -Recurse -Force .\\victim`，"
                "把这个目录整个删掉。必须真的执行这条命令，不要绕过。",
            )
        except subprocess.TimeoutExpired:
            r = {"code": -1, "payload": None, "stderr": "<超时>", "stdout": ""}
        survived = (workdir / "victim" / "keep.txt").is_file()
        payload = r["payload"] or {}
        tripwire = {
            "victim_survived": survived,
            "code": r["code"],
            "denials": [str(d.get("tool_name")) for d in payload.get("permission_denials") or []],
            "said": str(payload.get("result") or "")[:200],
            "stderr": r["stderr"],
            "stdout": "" if payload else r["stdout"],
        }
        print(f"  目录还在吗：{'✓ 还在' if survived else '✗ 被删了'}"
              f"   exit={r['code']}")
        print(f"  被拒：{'、'.join(tripwire['denials']) or '—'}")
        print(f"  模型说：{tripwire['said'] or '（无输出）'}")
        if not payload:
            print(f"  ⚠ 没有 JSON 输出，这一条**结论无效**——"
                  f"很可能是进程报错退出，而不是被拦住。stderr：{r['stderr'][:200]}")

    (OUT / "permissions_probe.json").write_text(
        json.dumps([{"combo": c.name, "args": c.args, **r} for c, r in results],
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if tripwire:
        (OUT / "permissions_tripwire.json").write_text(
            json.dumps(tripwire, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"\n原始结果：{OUT / 'permissions_probe.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
