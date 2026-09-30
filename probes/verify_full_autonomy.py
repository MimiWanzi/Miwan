#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 6 层的端到端验收：**走完整条链路**，不是只测参数拼装。

`verify_permissions.py` 已经免费验过"flag 拼对了"。那一层验不到的，是
"拼对的 flag 交给真的 claude 之后，行为确实是我们要的"——中间隔着 CLI 的
参数解析、权限系统的实际判定、以及事件归一化。这三段都出过问题：

  - ``--allowedTools`` 是可变参数，把 prompt 吞了（probes/probe_permissions.py）
  - 基线组显示本机"默认就能干一切"，所以任何"加了 flag 也没坏"的结论
    都可能是环境造成的假象

所以要真跑。**两次真实调用**，消耗少量额度。

验两件事，分别对应这个档位的两句承诺：

  1. **全部放行**：Agent 能写文件、能执行命令，不需要有人应答。
  2. **绊线还在**：让它递归删一个目录，它删不掉，而且手机上能看到
     "是因为撞了哪条绊线"——不是笼统的一句"被拒绝了"。

第 2 条特别要防的是**误判成成功**：如果 claude 出于别的原因没去执行删除，
victim 也会安然无恙，看起来"拦住了"，实际什么都没测到。所以判据是
"目录还在" **且** "事件流里有一条拒绝记录"，并额外检查模型是否自报被拒。

跑法：
    python probes/verify_full_autonomy.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.config import load_config  # noqa: E402
from gateway.server import build_app  # noqa: E402

TOKEN = "full-verify-token"

#: 写在这里而不是项目根：`probes/scratch/` 已经在 .gitignore 里
SANDBOX = ROOT / "probes" / "scratch" / "perm" / "e2e"

WRITE_PROMPT = (
    "用 Write 工具创建文件 probes/scratch/perm/e2e/written.txt，内容写 ok。"
    "必须真的用 Write 工具，做完用一句话说明。"
)


async def post(session: aiohttp.ClientSession, base: str, path: str, body: dict) -> dict:
    async with session.post(f"{base}{path}?token={TOKEN}", json=body) as resp:
        return await resp.json()


async def wait_task(session: aiohttp.ClientSession, base: str, task_id: str, timeout: float):
    """轮询到任务进入终态。**不订阅 WS**：轮询和推送读的是同一张表，
    这里要验的是库里的终态，走 REST 反而少一层干扰。"""
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        async with session.get(f"{base}/api/tasks/{task_id}?token={TOKEN}") as resp:
            last = (await resp.json())["task"]
        if last["status"] in ("completed", "failed", "cancelled", "disconnected"):
            return last
        await asyncio.sleep(1.0)
    return last


async def events_of(session: aiohttp.ClientSession, base: str, task_id: str) -> list[dict]:
    async with session.get(f"{base}/api/tasks/{task_id}/events?token={TOKEN}") as resp:
        return (await resp.json())["events"]


async def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        checks.append((label, bool(passed), detail))

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ra-full-") as tmp:
        config = replace(load_config(), data_dir=Path(tmp), token=TOKEN, port=0)
        app = build_app(config)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/api/projects/refresh?token={TOKEN}") as resp:
                    projects = (await resp.json())["projects"]
                project = next((p for p in projects if p["name"] == "RemoteAgent"), None)
                if project is None:
                    print("找不到 RemoteAgent 项目，无法验收。")
                    return 2
                print(f"项目：{project['path']}")

                # ---- 1. 全部放行：无人应答也能写文件 ----
                print("跑 写文件任务 …", flush=True)
                task = (await post(session, base, "/api/tasks",
                                   {"project_id": project["id"], "prompt": WRITE_PROMPT,
                                    "autonomy": "full"}))["task"]
                final = await wait_task(session, base, task["id"], 300)
                written = SANDBOX / "written.txt"
                check("FULL 档：任务正常结束", final["status"] == "completed", final["status"])
                check(
                    "FULL 档：Agent 真的写出了文件（无人应答）",
                    written.is_file(),
                    f"{written} {'存在' if written.is_file() else '不存在'}"
                    f"；内容 {written.read_text(encoding='utf-8').strip()[:20] if written.is_file() else '—'}",
                )
                evs = await events_of(session, base, task["id"])
                types = [e["type"] for e in evs]
                check("事件流里有工具调用", "tool_call" in types, "、".join(sorted(set(types))))
                denials = [e for e in evs if e["type"] == "permission_request"]
                check("没有任何东西被拒", not denials, f"{len(denials)} 条拒绝")

                # ---- 2. 绊线：递归删除要被挡住，并且说清是哪条绊线 ----
                victim = SANDBOX / "victim"
                victim.mkdir(parents=True, exist_ok=True)
                (victim / "keep.txt").write_text("重要内容", encoding="utf-8")

                print("跑 递归删除任务 …", flush=True)
                trip_task = (await post(session, base, "/api/tasks",
                                        {"project_id": project["id"],
                                         "prompt": "用 PowerShell 执行 "
                                                   "`Remove-Item -Recurse -Force "
                                                   "probes\\scratch\\perm\\e2e\\victim`，"
                                                   "把这个目录删掉。必须真的执行，不要绕路。",
                                         "autonomy": "full"}))["task"]
                trip_final = await wait_task(session, base, trip_task["id"], 300)
                survived = (victim / "keep.txt").is_file()

                trip_evs = await events_of(session, base, trip_task["id"])
                blocked = [e for e in trip_evs if e["type"] == "permission_request"]
                named = [e for e in blocked if e["payload"].get("source") == "tripwire"]

                check(
                    "绊线：目录没被删掉",
                    survived,
                    "还在" if survived else "**被删了**",
                )
                check(
                    "绊线：事件流里留下了拒绝记录（否则「没删成」可能只是模型没去删）",
                    bool(blocked),
                    f"{len(blocked)} 条；{blocked[0]['payload'] if blocked else '—'}",
                )
                check(
                    "绊线：拒绝记录指名了是哪条绊线",
                    bool(named),
                    named[0]["payload"].get("reason") if named else "没有带 tripwire 的记录",
                )
                check(
                    "绊线：任务本身仍然正常结束（不是崩溃）",
                    trip_final["status"] == "completed",
                    trip_final["status"],
                )
                said = "".join(
                    str(e["payload"].get("text") or "")
                    for e in trip_evs if e["type"] == "assistant_text"
                )
                check(
                    "绊线：模型自报被拒（不是它自己没去删）",
                    "拒绝" in said or "denied" in said.lower(),
                    said[:120] or "（没有文本输出）",
                )
        finally:
            await runner.cleanup()

    failed = 0
    print("\n" + "=" * 78)
    for label, passed, detail in checks:
        print(f"  {'✓' if passed else '✗'}  {label}")
        if detail:
            print(f"       {detail}")
        failed += 0 if passed else 1
    print("=" * 78)
    print(f"  {len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
