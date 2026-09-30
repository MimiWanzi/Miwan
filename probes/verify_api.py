#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 4 层验收：REST + WebSocket。

`verify_adapter.py` 验的是"适配代码有没有把 CLI 用对"；这个脚本验的是
"**HTTP 这一层的行为对不对**"——也就是手机真正会碰到的东西。

验的七件事，每一件都是一个具体的失败方式：

  1. 没 token 一律 401；``/api/health`` 除外；带 ``?token=`` 打开会种下 cookie，
     之后不带 token 也进得来（第 5 层前端就靠这个，它自己不用管认证）
  2. 启动时把上次遗留的 running 任务收成 disconnected——第 3 层写好的
     ``runner.recover()`` 到底接上没有
  3. 提交任务**立即返回**，事件是**陆续**到的（不是任务跑完一次性倒出来）
  4. 事件 seq 严格递增，且 WebSocket 收到的序列与 REST 查到的完全一致 ——
     两者必须是同一份数据，否则"补发"和"实时"迟早对不上
  5. 带 ``since`` 连上，补发的正好是 ``seq > since`` 的那些，一条不多一条不少
  6. 对已经结束的任务点取消返回 409，而不是假装成功
  7. 未知任务 id：REST 404，WebSocket 回一个 error 帧再关

数据库用临时目录，**不污染真库**；但会真实调用一次 Claude（只读任务），消耗额度。

用法：
    python probes/verify_api.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from gateway.config import load_config  # noqa: E402
from gateway.models import Task, TaskStatus, normalize_path  # noqa: E402
from gateway.server import build_app  # noqa: E402
from gateway.store import Store  # noqa: E402
from gateway.sync import discover, persist  # noqa: E402

TOKEN = "verify-token-1234567890"
PROMPT = "读一下 main.py，用一句话回答它是做什么的。"
TERMINAL_EVENTS = {"task_completed", "task_failed"}

#: 整个验收的上限。真实任务 + 一次断线补发，给足余量。
HARD_TIMEOUT = 300.0


def check(
    checks: list[tuple[str, bool, str]], label: str, passed: bool, detail: str = ""
) -> None:
    checks.append((label, bool(passed), detail))


async def collect(
    session: aiohttp.ClientSession,
    url: str,
    stop,
    *,
    idle: float = 2.0,
    hard: float = HARD_TIMEOUT,
) -> list[tuple[float, dict]]:
    """读 WebSocket 帧，每帧记下**墙钟**到达时刻。

    到达时刻是这个脚本的关键证据：任务跑完后一次性回放和真在实时推送，
    收到的帧一模一样，**只有时间能区分它们**。用墙钟而不是单调时钟，
    是因为要比对的 ``event.ts`` 是墙钟（同一台机器，没有时差问题）。

    ``stop(frames)`` 成立后再多收 ``idle`` 秒，把尾部帧（比如任务状态帧）收干净。
    """
    frames: list[tuple[float, dict]] = []
    settled = False
    deadline = time.time() + hard

    async with session.ws_connect(url) as ws:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            wait = min(idle if settled else remaining, remaining)
            try:
                msg = await ws.receive(timeout=wait)
            except (TimeoutError, asyncio.TimeoutError):
                if settled:
                    break
                continue
            if msg.type is not aiohttp.WSMsgType.TEXT:
                break
            try:
                frames.append((time.time(), json.loads(msg.data)))
            except ValueError:
                continue
            if not settled and stop(frames):
                settled = True
    return frames


def _terminal_seen(frames: list[tuple[float, dict]]) -> bool:
    return any(
        frame.get("type") == "event" and frame["event"]["type"] in TERMINAL_EVENTS
        for _, frame in frames
    )


async def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    with tempfile.TemporaryDirectory(prefix="ra-verify-") as tmp:
        config = replace(load_config(), data_dir=Path(tmp), token=TOKEN)

        # ------------------------------------------------------------------
        # 预置数据：让启动时的 recover() 有东西可收
        # ------------------------------------------------------------------
        store = Store(config.db_path)
        adapters, scan = await discover(config.blocked_paths)
        if not adapters:
            print("没有可用的 Agent 接入。先跑 `python -m gateway agents` 看看。")
            store.close()
            return 1

        persist(store, scan)
        project = next(
            (
                p
                for p in store.list_projects()
                if normalize_path(p.path) == normalize_path(str(ROOT))
            ),
            None,
        ) or store.ensure_project(str(ROOT))
        store.create_task(
            Task(
                id="t-stale",
                project_id=project.id,
                prompt="（验收用的假任务，不该被自动重跑）",
                status=TaskStatus.RUNNING,
            )
        )
        store.close()
        print(f"临时库已备好：{config.db_path}")
        print(f"项目     {project.name}  {project.path}")
        print("=" * 68)

        app = build_app(config)
        runner = web.AppRunner(app)
        try:
            await runner.setup()  # on_startup 在这里跑，recover 也在这里
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            base = f"http://127.0.0.1:{port}"
            ws_base = f"ws://127.0.0.1:{port}/ws"
            print(f"监听     {base}\n")

            # 默认 CookieJar 不接受 IP 地址来的 cookie，这里显式放开
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            ) as session:
                await _run_checks(session, base, ws_base, project, checks)

        finally:
            await runner.cleanup()

    print("\n" + "=" * 68)
    failed = 0
    for label, passed, detail in checks:
        mark = "✓" if passed else "✗"
        line = f"  {mark}  {label}"
        if not passed and detail:
            line += f"\n       → {detail}"
        print(line)
        failed += 0 if passed else 1
    print("=" * 68)
    print(f"  {len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


async def _run_checks(
    session: aiohttp.ClientSession,
    base: str,
    ws_base: str,
    project,
    checks: list[tuple[str, bool, str]],
) -> None:
    # ---- 认证 --------------------------------------------------------
    async with session.get(f"{base}/api/projects") as resp:
        check(checks, "无 token 访问 /api 返回 401", resp.status == 401, f"实际 {resp.status}")

    async with session.get(f"{base}/api/health") as resp:
        check(checks, "/api/health 免 token", resp.status == 200, f"实际 {resp.status}")

    async with session.get(f"{base}/api/projects?token={TOKEN}") as resp:
        cookie_header = resp.headers.get("Set-Cookie", "")
        check(checks, "带 ?token= 可以访问", resp.status == 200, f"实际 {resp.status}")
        check(
            checks,
            "这个响应里种下了 token cookie",
            "ra_token=" in cookie_header,
            cookie_header[:100] or "（没有 Set-Cookie）",
        )

    async with session.get(f"{base}/api/projects") as resp:
        check(
            checks,
            "之后只靠 cookie 就能进（前端不必自己管 token）",
            resp.status == 200,
            f"实际 {resp.status}",
        )

    # ---- 启动恢复 ----------------------------------------------------
    async with session.get(f"{base}/api/tasks/t-stale") as resp:
        stale = (await resp.json()).get("task") or {}
    check(
        checks,
        "启动时遗留的 running 任务被收成 disconnected",
        stale.get("status") == TaskStatus.DISCONNECTED.value,
        f"实际 {stale.get('status')}",
    )

    # ---- 刷新项目 ----------------------------------------------------
    async with session.post(f"{base}/api/projects/refresh") as resp:
        refreshed = await resp.json()
        check(checks, "POST /api/projects/refresh 成功", resp.status == 200, f"实际 {resp.status}")
    check(
        checks,
        "刷新扫到了 claude_code",
        "claude_code" in (refreshed.get("agents") or []),
        str(refreshed.get("agents")),
    )
    check(checks, "刷新后库里有项目", bool(refreshed.get("projects")), str(refreshed)[:200])

    # ---- 提交任务 ----------------------------------------------------
    async with session.post(
        f"{base}/api/tasks",
        json={"project_id": project.id, "prompt": PROMPT, "autonomy": "read_only"},
    ) as resp:
        created = await resp.json()
        check(
            checks,
            "POST /api/tasks 返回 202（立即返回，不等任务跑完）",
            resp.status == 202,
            f"HTTP {resp.status} {str(created)[:200]}",
        )

    task = created.get("task") or {}
    task_id = task.get("id")
    check(checks, "提交后立刻拿到 task id", bool(task_id), str(created)[:200])
    if not task_id:
        return

    started_at = time.time()

    # ---- 实时推送 ----------------------------------------------------
    live = await collect(session, f"{ws_base}?task_id={task_id}", _terminal_seen)

    hello = next((f for _, f in live if f.get("type") == "hello"), None)
    check(checks, "连上先收到 hello", hello is not None, str([f.get("type") for _, f in live][:6]))

    events = [f["event"] for _, f in live if f.get("type") == "event"]
    seqs = [e["seq"] for e in events]
    kinds = [e["type"] for e in events]

    check(checks, "收到了事件流", bool(events), f"{len(events)} 条")
    check(
        checks,
        "seq 严格递增",
        all(b > a for a, b in zip(seqs, seqs[1:])),
        str(seqs[:15]),
    )
    check(checks, "事件流里有 task_started", "task_started" in kinds, str(sorted(set(kinds))))
    check(
        checks,
        "事件流以 task_completed 收尾",
        kinds.count("task_completed") == 1,
        str(sorted(set(kinds))),
    )
    check(checks, "事件流里没有 raw 兜底（说明映射没漏）", "raw" not in kinds, str(kinds))

    # ---- 实时性 ------------------------------------------------------
    #
    # 帧序列本身**证明不了**实时推送：任务跑完后一次性回放，收到的帧一模一样。
    # 能区分的只有时间，而且要比的是"事件自己的产生时刻"和"我们收到它的时刻"——
    # 每条事件都带 ts（落库前打的），于是：
    #
    #   实时推送 → 每条的延迟都是几十毫秒，且**不随任务时长增长**
    #   事后回放 → 第 N 条事件在库里躺了 (现在 - ts) 那么久才被推过来
    #
    # 关键是这个判据**与任务跑多久无关**。早先这里写的是"首尾跨度 > 5 秒"，
    # 那是在赌任务够慢，第一次跑就被 2.8 秒读完 main.py 的 Claude 打脸了。
    lags = [
        t - f["event"]["ts"] for t, f in live if f.get("type") == "event"
    ]
    check(
        checks,
        "第一条事件是在它产生后 1 秒内收到的（实时，不是事后回放）",
        bool(lags) and lags[0] < 1.0,
        f"首条延迟 {lags[0]:.3f}s" if lags else "没有事件",
    )
    check(
        checks,
        "没有任何一条事件是隔了很久才推过来的",
        bool(lags) and max(lags) < 3.0,
        f"最大延迟 {max(lags):.3f}s" if lags else "没有事件",
    )
    span = (lags and (max(lags) - min(lags))) or 0.0
    print(f"  推送延迟：首条 {lags[0]:.3f}s · 最大 {max(lags):.3f}s · 抖动 {span:.3f}s")

    task_frames = [f["task"] for _, f in live if f.get("type") == "task"]
    final_status = task_frames[-1]["status"] if task_frames else (hello or {}).get("task", {}).get("status")
    check(checks, "WS 推来了终态任务帧", final_status == "completed", f"实际 {final_status}")

    # ---- 补发（断线重连走的就是这条路）--------------------------------
    mid = seqs[len(seqs) // 2]
    expected_tail = [s for s in seqs if s > mid]

    replay = await collect(
        session, f"{ws_base}?task_id={task_id}&since={mid}", _terminal_seen
    )
    replay_seqs = [f["event"]["seq"] for _, f in replay if f.get("type") == "event"]
    check(
        checks,
        "带 since 连上，补发的正好是 seq > since 的那些",
        replay_seqs == expected_tail,
        f"since={mid} 期望 {expected_tail} 实际 {replay_seqs}",
    )

    async with session.get(f"{base}/api/tasks/{task_id}/events?since={mid}") as resp:
        rest = await resp.json()
    check(
        checks,
        "REST 按 seq 查与 WS 补发完全一致",
        [e["seq"] for e in rest["events"]] == replay_seqs,
        f"REST {[e['seq'] for e in rest['events']]} vs WS {replay_seqs}",
    )
    check(
        checks,
        "last_seq 等于本批最后一条的 seq",
        rest["last_seq"] == (replay_seqs[-1] if replay_seqs else mid),
        f"last_seq={rest['last_seq']}",
    )

    async with session.get(f"{base}/api/tasks/{task_id}") as resp:
        final = (await resp.json()).get("task") or {}
    check(
        checks,
        "REST 里任务终态与事件流一致",
        final.get("status") == "completed",
        f"实际 {final.get('status')}",
    )

    # ---- 边界 --------------------------------------------------------
    async with session.post(f"{base}/api/tasks/{task_id}/cancel") as resp:
        check(
            checks,
            "对已结束的任务取消返回 409，而不是假装成功",
            resp.status == 409,
            f"实际 {resp.status}",
        )

    async with session.get(f"{base}/api/tasks/nope") as resp:
        check(checks, "未知任务 REST 返回 404", resp.status == 404, f"实际 {resp.status}")

    async with session.ws_connect(f"{ws_base}?task_id=nope") as ws:
        msg = await ws.receive(timeout=10)
        bad = json.loads(msg.data) if msg.type is aiohttp.WSMsgType.TEXT else {}
    check(checks, "未知任务 WS 先回 error 帧", bad.get("type") == "error", str(bad)[:200])

    # ---- 给人看的时间线 ----------------------------------------------
    print("  实时事件流（相对任务提交时刻）")
    print("  " + "─" * 64)
    for t, frame in live:
        offset = t - started_at
        if frame.get("type") == "event":
            event = frame["event"]
            payload = event["payload"]
            detail = (
                payload.get("name")
                or payload.get("text")
                or payload.get("error")
                or ""
            )
            # 延迟这一列是给人看的证据：每行都是零点几秒，说明是边发生边推的
            print(
                f"    #{event['seq']:<4}{offset:7.2f}s  +{t - event['ts']:5.3f}s  "
                f"{event['type']:<18}{str(detail)[:44]}"
            )
        else:
            print(f"    {'':6}{offset:7.2f}s  {'':8}[{frame.get('type')}]")
    print("  " + "─" * 64)
    arrival_span = (live[-1][0] - live[0][0]) if len(live) > 1 else 0.0
    print(f"  连接存活 {arrival_span:.1f}s · 共 {len(events)} 条事件 · 表头 +Ts 为推送延迟\n")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
