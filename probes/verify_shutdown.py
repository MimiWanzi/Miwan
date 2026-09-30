#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关停是否留下孤儿进程。

这是"网关变成常驻服务"引入的**新**风险：CLI 时代 `run` 一定跑完才退出，
进程不会留；服务时代 Ctrl-C 会在任务跑到一半时把它掐掉，
如果 close() 没走完，就会留下一个没人看着、继续跑、继续烧额度的 claude.exe。

验的是三件事，缺一不可（少了第二条，测试可能空过）：
  1. 关停前：任务确实在跑，而且确实有一个子进程
  2. 关停走的是 Ctrl-C 那条路（runner.cleanup → on_shutdown → runner.shutdown）
  3. 关停后：子进程没了，且库里那条任务落到了终态

会真实调用一次 Claude（只读、多步），消耗少量额度。

跑法：
    python probes/verify_shutdown.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import load_config  # noqa: E402
from gateway.server import build_app  # noqa: E402
from gateway.store import Store  # noqa: E402

PROMPT = (
    "用 Glob 列出 gateway 目录下所有的 .py 文件，然后逐个 Read，"
    "最后用一句话说明每个模块负责什么。不要修改任何文件。"
)

#: 关停前等多久，让它跑到一半。够长才有"中途掐断"可言。
RUN_BEFORE_SHUTDOWN = 7.0

#: close() 的宽限期是 2.0s（session.GRACE_EXIT），留 1.5s 余量。
#: 实测这个宽限期对**正在跑一轮**的 claude 必然耗光（它做完当前这轮才看 stdin），
#: 所以正常情况下关停应该是"2 秒左右"，而不是"立刻"或"5 秒"。
SHUTDOWN_BUDGET = 3.5


def claude_pids() -> set[int]:
    """当前活着的 claude.exe 进程号。

    用差集判断，不去猜哪个是"我们的"——开发者自己很可能也开着 Claude Code。
    """
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    pids: set[int] = set()
    for line in out.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "claude.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


async def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        checks.append((label, bool(passed), detail))

    baseline = claude_pids()
    print(f"关停前已存在的 claude.exe：{sorted(baseline) or '无'}")

    final = None
    with tempfile.TemporaryDirectory(prefix="ra-shutdown-") as tmp:
        config = replace(load_config(), data_dir=Path(tmp), token="tok")

        app = build_app(config)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}"
        print(f"网关起在 {base}")

        task_id = None  # 提交失败时下面仍然能走到"查不到"，而不是 NameError
        try:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            ) as session:
                async with session.post(f"{base}/api/projects/refresh?token=tok") as resp:
                    projects = (await resp.json())["projects"]
                project_id = next(p["id"] for p in projects if p["name"] == "RemoteAgent")

                async with session.post(
                    f"{base}/api/tasks", json={"project_id": project_id, "prompt": PROMPT}
                ) as resp:
                    task_id = (await resp.json())["task"]["id"]
                print(f"任务 {task_id} 已提交，等 {RUN_BEFORE_SHUTDOWN:g}s 让它跑起来…")
                await asyncio.sleep(RUN_BEFORE_SHUTDOWN)

                async with session.get(f"{base}/api/tasks/{task_id}") as resp:
                    live = (await resp.json())["task"]
                check("关停前任务确实在运行中", live["status"] == "running", f"实际 {live['status']}")

            during = claude_pids() - baseline
            check("关停前确实有一个 claude.exe 子进程", len(during) >= 1, f"新增 {sorted(during) or '无'}")

            print("触发关停（等同于 Ctrl-C）…")
            stopped_at = time.time()
            await runner.cleanup()  # on_shutdown → runner.shutdown → 各会话 close()
            # **立刻**记下来。等完下面那 3 秒再算就把等待也算进去了。
            cleanup_elapsed = time.time() - stopped_at
            print(f"关停返回，耗时 {cleanup_elapsed:.2f}s")

            await asyncio.sleep(3.0)  # 给强杀留出被操作系统真正回收的时间
            after = claude_pids() - baseline
            check("关停后没有留下孤儿 claude.exe", not after, f"还剩 {sorted(after)}")
            check(
                "关停没有等到宽限期耗光才返回",
                cleanup_elapsed < SHUTDOWN_BUDGET,
                f"{cleanup_elapsed:.2f}s（上限 {SHUTDOWN_BUDGET}s）",
            )

            store = Store(config.db_path)
            try:
                final = store.get_task(task_id) if task_id else None
            finally:
                store.close()
        finally:
            await runner.cleanup()

        check(
            "被关停的任务落到了终态，不会永远停在 running",
            final is not None and final.status.value in {"cancelled", "completed"},
            f"实际 {final.status.value if final else '（查不到）'}",
        )
        if final is not None:
            print(f"任务最终状态：{final.status.value}  reason={final.exit_reason}")

    print("\n" + "=" * 60)
    failed = 0
    for label, passed, detail in checks:
        print(f"  {'✓' if passed else '✗'}  {label}")
        if not passed and detail:
            print(f"       → {detail}")
        failed += 0 if passed else 1
    print("=" * 60)
    print(f"  {len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
