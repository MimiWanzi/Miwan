#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code headless 探针
=========================

目的：在写 Gateway 之前，用最小代价清掉方案里 6 项未验证的假设。
每一项都对应一个会决定架构的选择，猜错就要返工。

用法：
    python probes/probe_claude.py            # 跑全部探针
    python probes/probe_claude.py p1 p3      # 只跑指定的
    python probes/probe_claude.py --list     # 列出探针及说明
    python probes/probe_claude.py --dry-run  # 只打印将执行的命令，不真跑（零成本）
    python probes/probe_claude.py --yes      # 跳过交互确认

产出：
    probes/out/<探针名>.jsonl   原始事件流样本，可直接打开查看
    stdout                      人类可读的结论报告

成本：
    会真实调用 Anthropic API 并消耗额度。每个探针都带 --max-budget-usd 上限，
    全跑一遍的量级在 0.5 美元以内。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

# --------------------------------------------------------------------------
# 路径与环境
# --------------------------------------------------------------------------

PROBE_DIR = Path(__file__).resolve().parent
SCRATCH = PROBE_DIR / "scratch"      # 探针的工作目录，与真实项目隔离
OUT_DIR = PROBE_DIR / "out"          # 原始样本落盘位置

# claude CLI 是原生可执行文件，直接调用它，绕开 .cmd/.ps1 shim，
# 双工管道不会被 cmd.exe 干扰。
EXE_CANDIDATES = [
    Path(r"E:\ProgramFiles\nodejs\node_global\node_modules\@anthropic-ai\claude-code\bin\claude.exe"),
]

# Windows 上 Claude Code 的 shell 工具叫 PowerShell，**不叫 Bash**。
# 已实测：--tools Bash 会得到空工具集（init 里 "tools":[]），Agent 一个命令都执行不了。
# 本机实测可用工具集：
#   Task, CronCreate, CronDelete, CronList, DesignSync, Edit, EnterWorktree,
#   ExitWorktree, Glob, Grep, ListAgents, NotebookEdit, PowerShell, Read,
#   ReportFindings, ScheduleWakeup, SendMessage, Skill, TaskStop, WebFetch,
#   WebSearch, Workflow, Write
# 含义：原型书 16.10 列的危险命令样本（rm / sudo / git push）是 bash 语法，
#      Windows 下拦截规则要按 PowerShell 语法重写。
SHELL_TOOL = "PowerShell"


def find_claude() -> str:
    for p in EXE_CANDIDATES:
        if p.exists():
            return str(p)
    found = shutil.which("claude")
    if found:
        return found
    raise RuntimeError(
        "找不到 claude CLI。请确认已安装，或把路径加进 EXE_CANDIDATES。"
    )


def clean_env() -> dict:
    """清掉嵌套会话的标记变量。

    我们此刻正跑在 Claude Code 里面，这些变量会影响被拉起的子进程行为，
    让探针信号不干净。清掉后子进程表现为一个普通的独立会话。
    """
    env = os.environ.copy()
    for key in (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_SESSION_ID",
    ):
        env.pop(key, None)
    return env


# --------------------------------------------------------------------------
# 子进程工具
# --------------------------------------------------------------------------

def save_raw(name: str, lines: list[str]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def truncate(obj, limit: int = 500) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + f"... (+{len(s) - limit} 字符)"


def summarize(lines: list[str]) -> dict:
    """统计事件流里出现了哪些 type/subtype，各多少条。"""
    counts: dict[str, int] = {}
    samples: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            key = "<非 JSON 行>"
            counts[key] = counts.get(key, 0) + 1
            samples.setdefault(key, line[:500])
            continue
        if not isinstance(obj, dict):
            continue
        key = obj.get("type", "<无 type>")
        if obj.get("subtype"):
            key += f"/{obj['subtype']}"
        counts[key] = counts.get(key, 0) + 1
        samples.setdefault(key, truncate(obj))

        # assistant 消息里再往下钻一层，看 content block 的种类
        msg = obj.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        bk = f"   └ content: {block.get('type')}"
                        counts[bk] = counts.get(bk, 0) + 1
                        samples.setdefault(bk, truncate(block))
    return {"counts": counts, "samples": samples, "line_count": len(lines)}


async def read_stream(proc, timeout: float) -> tuple[list[str], bool]:
    """读 stdout 直到 EOF 或超时。返回 (行列表, 是否超时)。"""
    lines: list[str] = []
    timed_out = False
    try:
        async with asyncio.timeout(timeout):
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                lines.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
    except (TimeoutError, asyncio.TimeoutError):
        timed_out = True
    return lines, timed_out


async def spawn(args: list[str], cwd: Path):
    return await asyncio.create_subprocess_exec(
        find_claude(),
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=clean_env(),
    )


async def reap(proc, timeout: float = 10.0) -> int | None:
    """收尾：等进程退出，超时就杀掉。"""
    if proc.returncode is not None:
        return proc.returncode
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        return None


def base_flags(budget: str = "0.20") -> list[str]:
    return [
        "--output-format", "stream-json",
        "--verbose",
        "--max-budget-usd", budget,
    ]


# --------------------------------------------------------------------------
# 探针定义
# --------------------------------------------------------------------------

async def probe_p1(report):
    """P1 · 基础事件流格式

    验证：-p --output-format stream-json 到底吐出哪些 message type，
          tool_use / tool_result 的真实形状是什么。
    决定：Adapter 的归一化映射表怎么写。
    """
    report("命令", "claude -p <prompt> --output-format stream-json --verbose")
    args = ["-p", "用 Read 工具读一下 hello.txt，然后用一句话说它写了什么。",
            "--tools", "Read", *base_flags()]
    proc = await spawn(args, SCRATCH)
    started = time.time()
    lines, timed_out = await read_stream(proc, timeout=180)
    code = await reap(proc)

    settle(report, SCRATCH, "p1", lines, timed_out, code, started)

    info = summarize(lines)
    report("事件类型分布", "")
    for key, n in info["counts"].items():
        print(f"      {n:>3} × {key}")
        sample = info["samples"].get(key)
        if sample:
            print(f"            {sample}")

    types = {k.split("/")[0] for k in info["counts"] if not k.startswith("   └")}
    report("结论", f"共 {info['line_count']} 行，{len(types)} 种顶层 type：{sorted(types)}")
    report("待确认", "对比方案里预设的归一化类型，看是否有映射不上的事件。")


async def probe_p2(report):
    """P2 · 双工长连接

    验证：--input-format stream-json 是否支持一个进程处理多轮消息。
    决定：Gateway 是「每任务常驻一个进程」还是「每条消息起一个进程」。
          前者能保住多轮上下文、进程开销低；后者简单但慢。
    """
    report("命令", "claude -p --input-format stream-json --output-format stream-json --verbose")
    args = ["-p", "--input-format", "stream-json", *base_flags("0.30")]
    proc = await spawn(args, SCRATCH)

    async def send(text: str) -> None:
        payload = {"type": "user", "message": {"role": "user", "content": text}}
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def read_until_result(timeout: float) -> list[str]:
        got: list[str] = []
        try:
            async with asyncio.timeout(timeout):
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    got.append(line)
                    try:
                        if json.loads(line).get("type") == "result":
                            break
                    except json.JSONDecodeError:
                        pass
        except (TimeoutError, asyncio.TimeoutError):
            got.append('{"__probe__": "本轮读取超时"}')
        return got

    transcript: list[str] = []

    report("第 1 轮", "发送：记住数字 4271")
    await send("请记住一个数字：4271。只回复 OK。")
    turn1 = await read_until_result(180)
    transcript += turn1
    alive_1 = proc.returncode is None
    results_1 = sum(1 for l in turn1 if _is_type(l, "result"))
    report("第 1 轮结果", f"收到 {results_1} 个 result，进程存活={alive_1}")

    report("第 2 轮", "发送：我刚才让你记的数字是多少？")
    await send("我刚才让你记住的数字是多少？只回复这个数字。")
    turn2 = await read_until_result(180)
    transcript += turn2
    alive_2 = proc.returncode is None
    results_2 = sum(1 for l in turn2 if _is_type(l, "result"))
    report("第 2 轮结果", f"收到 {results_2} 个 result，进程存活={alive_2}")

    recalled = "4271" in "\n".join(turn2)
    report("上下文是否跨轮保持", "是" if recalled else "否")

    # 关闭 stdin 看进程是否正常退出
    try:
        proc.stdin.close()
    except Exception:
        pass
    code = await reap(proc, timeout=15)

    settle(report, SCRATCH, "p2", transcript, False, code, None)
    report("结论",
           f"双工={'成立' if (alive_1 and results_1 == 1 and results_2 == 1) else '不成立'}；"
           f"跨轮记忆={'成立' if recalled else '不成立'}")
    report("待确认", "若双工成立，Gateway 每任务常驻一个进程即可。")


def _is_type(line: str, want: str) -> bool:
    try:
        return json.loads(line).get("type") == want
    except json.JSONDecodeError:
        return False


def _tool_called(line: str, name: str) -> bool:
    """判断这一行是不是对某个工具的 tool_use 调用。

    用途：区分「操作被拒绝」与「Agent 压根没动手」——上一轮探针混淆了这两者。
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    content = (obj.get("message") or {}).get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == name
        for b in content
    )


async def probe_p3(report):
    """P3 · 权限提示的真实行为（最关键的一项）

    验证：-p 模式下不给 host，Claude Code 遇到需要授权的工具会怎样？
          --permission-prompts none 是否真的自动拒绝？host 无人应答会挂死吗？
    决定：权限通道（permissions.py）是整个绕开、还是必须实现。
    """
    target = SCRATCH / "p3_test.txt"
    verdicts = {}

    for mode, label in (("none", "无人应答（none）"), ("host", "等 host 应答但无人实现（host）")):
        if target.exists():
            target.unlink()
        report(label, "让 Agent 执行 shell 命令写文件，观察权限行为")
        args = [
            "-p", "请执行这条 shell 命令：Set-Content -Path p3_test.txt -Value probe",
            "--tools", SHELL_TOOL,
            "--permission-mode", "default",
            "--permission-prompts", mode,
            *base_flags(),
        ]
        report("命令", " ".join(args))
        proc = await spawn(args, SCRATCH)
        started = time.time()
        lines, timed_out = await read_stream(proc, timeout=120)
        code = await reap(proc)
        settle(report, SCRATCH, f"p3_{mode}", lines, timed_out, code, started)

        # 关键：先确认 Agent 到底有没有调用到这个工具。
        # 上一轮探针就是因为 --tools Bash 拿到空集，Agent 根本没动手，
        # 却被脚本误判成「被拒绝」。
        called = any(_tool_called(l, SHELL_TOOL) for l in lines)

        denials = []
        subtype = None
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                subtype = obj.get("subtype")
                denials = obj.get("permission_denials") or []

        verdicts[mode] = {
            "调用了 shell 工具": called,
            "超时": timed_out,
            "文件被创建": target.exists(),
            "result subtype": subtype,
            "被拒次数": len(denials),
        }
        report("观察", json.dumps(verdicts[mode], ensure_ascii=False))

    report("对照表", "")
    for mode, v in verdicts.items():
        print(f"      [{mode}] {json.dumps(v, ensure_ascii=False)}")

    # 没有真正调用工具，说明这次探测没测到东西，不能下任何结论
    if not any(v["调用了 shell 工具"] for v in verdicts.values()):
        report("结论", f"两个分支都没有调用到 {SHELL_TOOL} → 本次探测**无效**，"
                       f"不能据此判断权限行为。先确认工具名是否正确。")
        return

    none_v = verdicts["none"]
    if none_v["被拒次数"] or not none_v["文件被创建"]:
        report("结论 1", "none：本该提示的操作没有完成 → "
                         "「选 none = Agent 干不了活」的判断成立。")
    else:
        report("结论 1", "none：操作竟然成功了 → 该操作不在权限管辖范围内，"
                         "需要一个真正会触发提示的操作才能测到。")

    host_v = verdicts["host"]
    if host_v["超时"]:
        report("结论 2", "host：无人应答会挂死 → 权限通道必须实现，绕不开。")
    elif not host_v["文件被创建"]:
        report("结论 2", "host：无人应答时操作未完成，但也没挂死 → "
                         "读原始样本确认是被静默拒绝还是被跳过了。")
    else:
        report("结论 2", "host：无人应答也成功了 → 该操作被自动放行，"
                         "没有触发真正意义上的人工确认。")


async def probe_p4(report):
    """P4 · 预分配会话 ID 与续接

    验证：--session-id 能否指定一个我们自己的 UUID；之后 --resume 能否接上。
    决定：Gateway 的 session 表能否从一开始就掌握 native session id，
          而不必去解析输出或扫 JSONL。
    """
    sid = str(uuid.uuid4())
    report("预分配 session id", sid)

    report("第 1 次调用", "写入记忆，指定 session-id")
    args1 = ["-p", "请记住数字 4271。只回复 OK。",
             "--session-id", sid, "--output-format", "json", "--max-budget-usd", "0.10"]
    proc = await spawn(args1, SCRATCH)
    lines1, to1 = await read_stream(proc, timeout=120)
    code1 = await reap(proc)
    settle(report, SCRATCH, "p4_first", lines1, to1, code1, None)
    report("第 1 次返回的 session_id", _extract_session_id(lines1) or "（未取到）")

    report("第 2 次调用", "以 --resume 续接，验证是否记得")
    args2 = ["-p", "我刚才让你记住的数字是多少？只回复这个数字。",
             "--resume", sid, "--output-format", "json", "--max-budget-usd", "0.10"]
    proc = await spawn(args2, SCRATCH)
    lines2, to2 = await read_stream(proc, timeout=120)
    code2 = await reap(proc)
    settle(report, SCRATCH, "p4_resume", lines2, to2, code2, None)

    recalled = "4271" in "\n".join(lines2)
    report("续接是否记得上下文", "是" if recalled else "否")

    # 会话文件是否真的落在预期位置
    proj = Path(os.path.expanduser("~")) / ".claude" / "projects"
    hits = list(proj.glob(f"*/{sid}.jsonl")) if proj.exists() else []
    report("会话文件位置", str(hits[0]) if hits else "未找到（可能落盘路径规则不同）")

    report("结论", f"预分配={'可用' if hits else '存疑'}；"
                   f"resume 续接={'可用' if recalled else '不可用'}")


async def probe_p5(report):
    """P5 · 并发

    验证：同时跑多个 claude 进程是否互相干扰。
    决定：runner.py 能否允许并发任务，还是要加全局串行锁。
    """
    prompt = "只回复一个数字 1，不要做别的。"
    report("并发数", "2")

    async def one(tag: str):
        args = ["-p", prompt, "--tools", "", *base_flags("0.10")]
        t0 = time.time()
        proc = await spawn(args, SCRATCH)
        lines, timed_out = await read_stream(proc, timeout=180)
        code = await reap(proc)
        return tag, lines, timed_out, code, time.time() - t0

    results = await asyncio.gather(one("A"), one("B"))
    ok = 0
    for tag, lines, timed_out, code, elapsed in results:
        settle(report, SCRATCH, f"p5_{tag}", lines, timed_out, code, None)
        good = (not timed_out) and any(_is_type(l, "result") for l in lines)
        ok += 1 if good else 0
        report(f"进程 {tag}", f"成功={good} 耗时={elapsed:.1f}s")

    report("结论", f"{ok}/2 成功 → {'可并发' if ok == 2 else '需进一步排查，可能要串行'}")


def _extract_session_id(lines: list[str]) -> str | None:
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("session_id"):
            return obj["session_id"]
    return None


def settle(report, scratch: Path, name: str, lines, timed_out, code, started):
    """统一的收尾：落盘原始样本 + 报告退出状态。"""
    path = save_raw(name, lines)
    n = len([l for l in lines if l.strip()])
    status = []
    status.append(f"{n} 行")
    status.append("超时" if timed_out else "正常结束")
    if code is not None:
        status.append(f"退出码={code}")
    if started:
        status.append(f"耗时={time.time() - started:.1f}s")
    report("原始样本", f"{path}  ({', '.join(status)})")


PROBES = {
    "p1": ("基础事件流格式", probe_p1),
    "p2": ("双工长连接（一进程多轮）", probe_p2),
    "p3": ("权限提示行为（最关键）", probe_p3),
    "p4": ("预分配 session id 与续接", probe_p4),
    "p5": ("并发多进程", probe_p5),
}


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Claude Code headless 探针",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("names", nargs="*", help="要跑的探针，默认全部")
    parser.add_argument("--list", action="store_true", help="列出探针")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不真跑（零成本）")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    opts = parser.parse_args()

    if opts.list:
        print("可用探针：")
        for key, (desc, _) in PROBES.items():
            print(f"  {key}  {desc}")
        return 0

    selected = opts.names or list(PROBES)
    unknown = [n for n in selected if n not in PROBES]
    if unknown:
        print(f"未知探针：{unknown}。用 --list 查看可用项。", file=sys.stderr)
        return 2

    SCRATCH.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "hello.txt").write_text("probe 用的样本文件\n", encoding="utf-8")

    exe = find_claude()
    print("=" * 74)
    print("Claude Code headless 探针")
    print("=" * 74)
    print(f"CLI      : {exe}")
    print(f"工作目录 : {SCRATCH}")
    print(f"样本输出 : {OUT_DIR}")
    print(f"将运行   : {', '.join(selected)}")
    print()
    print("注意：会真实调用 Anthropic API 并消耗额度（全跑一遍 < 0.5 美元）。")
    print("      会在 ~/.claude/projects/ 下产生真实会话记录。")
    print("=" * 74)

    if opts.dry_run:
        print("\n[--dry-run] 仅列出将执行的命令，未真正运行。\n")
        for name in selected:
            print(f"  {name}: {PROBES[name][0]}")
            print(f"      {exe} <见方案各探针定义>")
        return 0

    if not opts.yes and sys.stdin.isatty():
        reply = input("\n继续？(y/N) ").strip().lower()
        if reply not in ("y", "yes"):
            print("已取消。")
            return 1

    async def runner():
        for name in selected:
            desc, fn = PROBES[name]
            print()
            print("─" * 74)
            print(f"  {name} · {desc}")
            print("─" * 74)

            def report(label: str, value: str) -> None:
                print(f"  [{label}] {value}")

            try:
                await fn(report)
            except Exception as exc:  # 探针失败不应中断其余探针
                report("探针异常", f"{type(exc).__name__}: {exc}")

    asyncio.run(runner())

    print()
    print("=" * 74)
    print(f"完成。原始样本在 {OUT_DIR}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
