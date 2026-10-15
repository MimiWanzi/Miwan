#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 8 层的免费验证：断线补发。

**不花一分钱**——事件由测试直接写进库，不启动任何 Agent。
这恰恰是这一层需要的能力：真实 Agent 的事件**又快又不可控**，
而断线补发要验的偏偏是边界（缺口精确、分页、丢弃后能捞回来、重连幂等），
那些边界用真 Agent 造不出来。

跑法：
    python probes/verify_reconnect.py
    python probes/verify_reconnect.py -v

验收目标一句话就能说清："手机和电脑之间不能形成强绑定……Task continues"。
翻译成可验证的话是**四条**，下面每一段对着一条：

  1. 客户端走不走，任务照跑（断线期间产生的事件一条都不能丢）
  2. 重连是**精确续接**，不是重放：不能重、不能漏
  3. 慢消费者不该导致**静默丢数据**（这是最容易写错、也最难发现的一条）
  4. "状态正确"——重连后看到的状态是**当前**状态，不是连接建立那一刻的

第 3 条值得展开：总线在队列满时会丢最旧的事件（``EventBus.publish``）。
如果补发是"把队列里的东西发出去"，那么抖动一下就会永久丢事件，
而且丢得毫无痕迹。这里的设计是**补发一律重新查库**，队列只是唤醒信号。
下面 ``phase_slow_consumer`` 就是把这个设计钉住。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.config import load_config  # noqa: E402
from gateway.context import CTX  # noqa: E402
from gateway.events import Event, EventBus, EventType  # noqa: E402
from gateway.models import Project, Task, TaskStatus  # noqa: E402
from gateway.server import build_app  # noqa: E402
from gateway.store import Store  # noqa: E402
from gateway.ws import WS_BATCH  # noqa: E402

HOST_TOKEN = "host-token-for-tests"

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

Checks = list[tuple[str, bool, str]]


def ok(checks: Checks, label: str, passed: bool, detail: str = "") -> None:
    checks.append((label, passed, detail))


# ----------------------------------------------------------------------
# harness
# ----------------------------------------------------------------------


class Server:
    """一个临时网关。事件由测试自己灌，不起 Agent。"""

    def __init__(self, data_dir: Path) -> None:
        self.config = replace(
            load_config(),
            data_dir=data_dir,
            token=HOST_TOKEN,
            web_dir=ROOT / "nonexistent-web",
        )
        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.base = ""

    async def __aenter__(self) -> "Server":
        self.app = build_app(self.config)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def __aexit__(self, *exc) -> None:
        assert self.runner is not None
        await self.runner.cleanup()

    @property
    def ctx(self):
        assert self.app is not None
        return self.app[CTX]

    def make_task(self, task_id: str, status: TaskStatus = TaskStatus.RUNNING) -> str:
        """造一条任务行。不经过 runner——这里验的是流，不是调度。

        项目要一起建：``tasks.project_id`` 有外键，光插任务会被 SQLite 拒掉。
        """
        self._ensure_project("p-test")
        self.ctx.store.create_task(
            Task(
                id=task_id,
                project_id="p-test",
                prompt="断线补发用的假任务",
                status=status,
                created_at=1000.0,
                started_at=1000.0,
            )
        )
        return task_id

    def _ensure_project(self, project_id: str) -> None:
        if self.ctx.store.get_project(project_id) is None:
            self.ctx.store.upsert_project(
                Project(id=project_id, name="断线补发测试项目", path=str(self.config.data_dir))
            )

    def sink(self, task_id: str):
        """**生产代码里那个 emit**，不是测试自己拼的。

        直接调 ``store.append_event`` + ``bus.publish`` 也能跑，但那样
        "先落库拿 seq 再广播"这个顺序就没人看着了，而它正是去重的依据。
        """
        return self.ctx.runner._sink(task_id)

    def inject(self, task_id: str, n: int, prefix: str = "e") -> list[int]:
        """灌 n 条事件，返回它们的 seq。"""
        emit = self.sink(task_id)
        seqs = []
        for i in range(n):
            event = Event(task_id=task_id, type=EventType.ASSISTANT_TEXT, payload={"i": i, "p": prefix})
            emit(event)
            seqs.append(event.seq)
        return seqs


async def ws_url(server: Server, task_id: str, since: int) -> str:
    return f"{server.base}/ws?task_id={task_id}&since={since}"


async def open_ws(sess, server: Server, task_id: str, since: int):
    ws = await sess.ws_connect(
        await ws_url(server, task_id, since),
        headers={"X-Token": HOST_TOKEN},
        timeout=10,
    )
    return ws


async def read_until_hello(ws) -> dict:
    """等到 hello 帧为止。

    必须在灌事件**之前**等它：hello 是在 ``bus.subscribe()`` 之后才发的，
    所以收到 hello 就等于"订阅已经建立"。不等的话可能灌在订阅之前，
    那条事件就只能等 15 秒的空转（``WS_TICK``）才被查到——测试会变慢，
    而且慢得看起来像 bug。
    """
    while True:
        msg = await ws.receive(timeout=10)
        if msg.type is WSMsgType.TEXT:
            frame = json.loads(msg.data)
            if frame.get("type") == "hello":
                return frame
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
            raise AssertionError(f"连接在 hello 之前就断了：{msg.type} {msg.data!r}")


async def drain_events(ws, count: int, timeout: float = 10.0) -> list[dict]:
    """收满 count 条 event 帧（忽略 task / pong）。

    收不满就返回已经收到的，**不抛超时异常**：有几处检查要的正是
    "一条都不该来"（重连幂等），那种情况下"安静地什么都没收到"是
    **通过**，而不是错误。超时在这里是结果，不是故障。
    """
    got: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while len(got) < count:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            break
        try:
            msg = await ws.receive(timeout=left)
        except (TimeoutError, asyncio.TimeoutError):
            break
        if msg.type is WSMsgType.TEXT:
            frame = json.loads(msg.data)
            if frame.get("type") == "event":
                got.append(frame["event"])
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
            break
    return got


async def rest_events(sess, server: Server, task_id: str, since: int, limit: int = 1000):
    async with sess.get(
        f"{server.base}/api/tasks/{task_id}/events",
        params={"since": since, "limit": limit},
        headers={"X-Token": HOST_TOKEN},
    ) as resp:
        return resp.status, await resp.json()


async def session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


# ----------------------------------------------------------------------
# 1. REST 补发的边界
# ----------------------------------------------------------------------


async def phase_rest_replay(checks: Checks) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            task_id = server.make_task("t-rest")
            seqs = server.inject(task_id, 7)

            async with await session() as sess:
                # since=0 拿到全部 —— 这就是"关掉页面再打开，事件不丢"
                status, body = await rest_events(sess, server, task_id, 0)
                ok(checks, "REST since=0 返回全部事件", status == 200 and len(body["events"]) == 7,
                   f"status={status} n={len(body['events'])}")

                # last_seq 是最后一条的 seq。客户端拿它当下一个 since。
                ok(checks, "REST last_seq = 最后一条的 seq",
                   body["last_seq"] == seqs[-1], f"{body['last_seq']} vs {seqs[-1]}")

                # 游标在末尾：回空，但 **last_seq 必须回 since 而不是 0**。
                # 回 0 的话客户端会把游标倒回去，把整个历史重收一遍。
                status, body = await rest_events(sess, server, task_id, seqs[-1])
                ok(checks, "REST since=末尾 回空且 last_seq 保持不回退",
                   body["events"] == [] and body["last_seq"] == seqs[-1],
                   f"last_seq={body['last_seq']}")

                # 中间游标：只给严格大于它的
                status, body = await rest_events(sess, server, task_id, seqs[2])
                got = [e["seq"] for e in body["events"]]
                ok(checks, "REST since=中间 只返回严格更大的 seq",
                   got == seqs[3:], f"{got} vs {seqs[3:]}")

                # 分页：limit=3 时三页拼起来必须等于整体，不多不少
                joined: list[int] = []
                cursor = 0
                for _ in range(10):
                    _, body = await rest_events(sess, server, task_id, cursor, limit=3)
                    if not body["events"]:
                        break
                    assert body["events"][-1]["seq"] > cursor, "游标没前进，会死循环"
                    cursor = body["last_seq"]
                    joined += [e["seq"] for e in body["events"]]
                    if not body["has_more"]:
                        break
                ok(checks, "REST 分页拼接 = 整体（无重无漏）", joined == seqs, f"{joined} vs {seqs}")

                # 未来游标不能崩，也不能回 0
                status, body = await rest_events(sess, server, task_id, 10**9)
                ok(checks, "REST since=未来 不报错且回空",
                   status == 200 and body["events"] == [], f"status={status}")

                # 非法 since 退回默认值而不是 400（查询参数写错不该炸掉页面）
                status, body = await rest_events(sess, server, task_id, -5)
                ok(checks, "REST since=负数 被夹到 0（退回全部）",
                   status == 200 and len(body["events"]) == 7, f"status={status}")

                # 未知任务 404，不是空列表
                status, _ = await rest_events(sess, server, "t-nope", 0)
                ok(checks, "REST 未知任务 = 404", status == 404, f"status={status}")


# ----------------------------------------------------------------------
# 2. 实时推送
# ----------------------------------------------------------------------


async def phase_live(checks: Checks) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            task_id = server.make_task("t-live")

            async with await session() as sess:
                ws = await open_ws(sess, server, task_id, 0)
                await read_until_hello(ws)

                # 订阅建立之后再灌
                seqs = server.inject(task_id, 5)
                got = await drain_events(ws, 5)
                ok(checks, "WS 实时推送收满 5 条", len(got) == 5, f"n={len(got)}")
                ok(checks, "WS 实时推送 seq 严格递增",
                   [e["seq"] for e in got] == seqs, f"{[e['seq'] for e in got]} vs {seqs}")
                ok(checks, "WS 实时推送内容未串（payload 对得上）",
                   [e["payload"]["i"] for e in got] == [0, 1, 2, 3, 4],
                   f"{[e['payload']['i'] for e in got]}")
                await ws.close()


# ----------------------------------------------------------------------
# 3. 断线期间的缺口能精确补上（断线恢复的核心）
# ----------------------------------------------------------------------


async def phase_gap(checks: Checks) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            task_id = server.make_task("t-gap")

            async with await session() as sess:
                # --- 连着，看一眼，然后**拔线** ---
                ws = await open_ws(sess, server, task_id, 0)
                await read_until_hello(ws)
                before = server.inject(task_id, 3, prefix="before")
                got = await drain_events(ws, 3)
                last_seq = got[-1]["seq"]
                await ws.close()

                # --- 断线期间任务照跑，事件照落库 ---
                during = server.inject(task_id, 5, prefix="during")

                # --- 重连：带 since=lastSeq（前端存的就是这个） ---
                ws = await open_ws(sess, server, task_id, last_seq)
                hello = await read_until_hello(ws)
                ok(checks, "重连 hello.since 回显客户端给的游标",
                   hello["since"] == last_seq, f"{hello['since']} vs {last_seq}")

                got = await drain_events(ws, 5)
                got_seqs = [e["seq"] for e in got]

                ok(checks, "断线缺口**精确**补上（无重无漏）",
                   got_seqs == during, f"{got_seqs} vs {during}")
                ok(checks, "补发不含断线前已收到的（没重发历史）",
                   all(s > last_seq for s in got_seqs), f"last_seq={last_seq} got={got_seqs}")
                ok(checks, "补发内容确实是断线期产生的那批",
                   [e["payload"]["p"] for e in got] == ["during"] * 5,
                   f"{[e['payload']['p'] for e in got]}")

                # 补完之后再重连一次同样的游标 -> 一条都不该有（幂等）
                await ws.close()
                ws = await open_ws(sess, server, task_id, during[-1])
                await read_until_hello(ws)
                again = await drain_events(ws, 1, timeout=1.0)
                ok(checks, "同一游标重连两次 = 幂等（第二次为空）",
                   again == [], f"n={len(again)}")
                await ws.close()


# ----------------------------------------------------------------------
# 4. 慢消费者被丢队列，也必须能补回来
# ----------------------------------------------------------------------


async def phase_slow_consumer(checks: Checks) -> None:
    """总线丢事件 ≠ 数据丢了。

    ``EventBus.publish`` 在队列满时会 ``get_nowait()`` 丢掉最旧的一条——
    这是**故意的**，慢客户端不允许拖住 Agent。但前提是"补发重新查库"，
    否则这里丢掉的就是永久丢失。
    """
    with tempfile.TemporaryDirectory() as tmp:
        server_data = Path(tmp)
        store = Store(server_data / "gateway.db")
        bus = EventBus(queue_size=10)  # 故意调小，好触发丢弃

        task_id = "t-slow"
        store.upsert_project(Project(id="p", name="x", path=str(server_data)))
        store.create_task(
            Task(id=task_id, project_id="p", prompt="x", status=TaskStatus.RUNNING,
                 created_at=1.0)
        )

        queue = bus.subscribe()
        emit = lambda e: (store.append_event(e), bus.publish(e))  # noqa: E731

        total = 50
        for i in range(total):
            emit(Event(task_id=task_id, type=EventType.ASSISTANT_TEXT, payload={"i": i}))

        ok(checks, "总线确实丢了事件（队列只有 10，发了 50）",
           queue.qsize() == 10, f"qsize={queue.qsize()}")

        # 队列里只剩最后 10 条，但库里是 50 条。
        # 客户端补发的依据是**库**，所以它能拿到全部 50 条。
        from_db = store.events_since(task_id, 0, limit=1000)
        ok(checks, "丢弃后库里仍是完整 50 条（丢的是队列，不是数据）",
           len(from_db) == total, f"n={len(from_db)}")
        ok(checks, "补发的顺序与原始一致",
           [e.payload["i"] for e in from_db] == list(range(total)))
        ok(checks, "被队列丢弃的最旧那条仍在补发结果里（不会静默丢）",
           from_db[0].payload["i"] == 0, f"first={from_db[0].payload['i']}")

        # 队列里剩的恰好是最后 10 条——证明丢的是最旧的
        drained = [queue.get_nowait().payload["i"] for _ in range(queue.qsize())]
        ok(checks, "队列丢的是最旧的（保留窗口是尾部）",
           drained == list(range(total - 10, total)), f"{drained}")

        bus.unsubscribe(queue)
        store.close()


# ----------------------------------------------------------------------
# 5. 超过单批上限的分页补发
# ----------------------------------------------------------------------


async def phase_batch(checks: Checks) -> None:
    """断线一小时再连回来可能有几千条。``WS_BATCH`` 之外要走多轮。"""
    n = WS_BATCH + 300
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            task_id = server.make_task("t-batch")
            server.inject(task_id, n)

            async with await session() as sess:
                ws = await open_ws(sess, server, task_id, 0)
                await read_until_hello(ws)
                got = await drain_events(ws, n, timeout=60)
                ok(checks, f"一次补发 {n} 条（> WS_BATCH={WS_BATCH}）全部到达",
                   len(got) == n, f"n={len(got)}")
                ok(checks, "跨批补发的 seq 严格递增且无重",
                   [e["seq"] for e in got] == sorted({e["seq"] for e in got}),
                   f"n={len(got)} uniq={len({e['seq'] for e in got})}")
                await ws.close()

                # 中途接入也一样：从中间游标补 1000 条，边界不能错位
                start = got[n // 2]["seq"]
                ws = await open_ws(sess, server, task_id, start)
                await read_until_hello(ws)
                rest = await drain_events(ws, n - n // 2 - 1, timeout=60)
                ok(checks, "从中间游标跨批补发边界不重不漏",
                   [e["seq"] for e in rest] == [e["seq"] for e in got if e["seq"] > start],
                   f"n={len(rest)}")
                await ws.close()


# ----------------------------------------------------------------------
# 6. 多个订阅者
# ----------------------------------------------------------------------


async def phase_multi_subscriber(checks: Checks) -> None:
    """"不假设只有一个连接"——这条是把局域网换成隧道的前提之一。"""
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            task_id = server.make_task("t-multi")

            async with await session() as sess:
                a = await open_ws(sess, server, task_id, 0)
                b = await open_ws(sess, server, task_id, 0)
                await read_until_hello(a)
                await read_until_hello(b)

                seqs = server.inject(task_id, 4)
                got_a = await drain_events(a, 4)
                got_b = await drain_events(b, 4)
                ok(checks, "两个订阅者都收满",
                   len(got_a) == 4 and len(got_b) == 4, f"{len(got_a)}/{len(got_b)}")
                ok(checks, "两个订阅者收到**相同**的序列",
                   [e["seq"] for e in got_a] == [e["seq"] for e in got_b] == seqs,
                   f"a={[e['seq'] for e in got_a]} b={[e['seq'] for e in got_b]}")

                # 第三个连接从一个**旧**游标接入：不影响已经在的两个
                c = await open_ws(sess, server, task_id, seqs[0])
                await read_until_hello(c)
                got_c = await drain_events(c, 3)
                ok(checks, "后到的订阅者从旧游标补齐，不影响先到的",
                   [e["seq"] for e in got_c] == seqs[1:], f"{[e['seq'] for e in got_c]}")

                for ws in (a, b, c):
                    await ws.close()


# ----------------------------------------------------------------------
# 7. 重连后的状态正确性
# ----------------------------------------------------------------------


async def phase_status(checks: Checks) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            task_id = server.make_task("t-status", TaskStatus.RUNNING)

            async with await session() as sess:
                ws = await open_ws(sess, server, task_id, 0)
                hello = await read_until_hello(ws)
                ok(checks, "hello 带着任务对象（客户端不用额外请求就能画状态）",
                   hello["task"]["id"] == task_id and hello["task"]["status"] == "running",
                   f"{hello['task'].get('status')}")
                await ws.close()

                # --- 断线期间任务跑完了 ---
                server.inject(task_id, 2)
                server.ctx.store.update_task(
                    task_id, status=TaskStatus.COMPLETED, finished_at=1234.0
                )

                # --- 重连：拿到的必须是**当前**状态，不是连接建立那刻的 ---
                ws = await open_ws(sess, server, task_id, 0)
                hello = await read_until_hello(ws)
                ok(checks, "重连后 hello 给的是当前状态（completed）",
                   hello["task"]["status"] == "completed", f"{hello['task']['status']}")
                ok(checks, "重连后 finished_at 有值（页面能算耗时）",
                   hello["task"]["finished_at"] == 1234.0, f"{hello['task'].get('finished_at')}")

                got = await drain_events(ws, 2)
                ok(checks, "重连补齐断线期间最后产生的事件",
                   len(got) == 2, f"n={len(got)}")

                # 状态变化要作为 task 帧推出去（不用等客户端轮询）
                server.ctx.store.update_task(task_id, status=TaskStatus.FAILED, error="boom")
                frame = await wait_for_frame(ws, "task", timeout=20)
                ok(checks, "状态变化会作为 task 帧主动推送",
                   frame is not None and frame["task"]["status"] == "failed",
                   f"{frame and frame['task'].get('status')}")
                ok(checks, "task 帧带着出错原因",
                   frame is not None and frame["task"].get("error") == "boom",
                   f"{frame and frame['task'].get('error')}")
                await ws.close()

                # 终态之后连接**保持**：服务端主动断开会让前端以为出问题了
                ws = await open_ws(sess, server, task_id, 0)
                await read_until_hello(ws)
                ok(checks, "任务终态之后连接不被服务端主动关掉",
                   not ws.closed, f"closed={ws.closed}")
                await ws.close()


async def wait_for_frame(ws, kind: str, timeout: float = 10.0) -> dict | None:
    """等某一类帧。用来验"状态变化会主动推"，而不是等客户端来问。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        left = deadline - loop.time()
        if left <= 0:
            return None
        msg = await ws.receive(timeout=left)
        if msg.type is WSMsgType.TEXT:
            frame = json.loads(msg.data)
            if frame.get("type") == kind:
                return frame
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
            return None


# ----------------------------------------------------------------------
# 8. WS 只是优化：两条路给出同一条序列
# ----------------------------------------------------------------------


async def phase_ws_equals_rest(checks: Checks) -> None:
    """``ws.py`` 的 docstring 敢写"WebSocket 只是优化，不是必需"，这里把它钉住。

    如果哪天有人往 WS 那条路里加一步"只在推送时做"的处理（补字段、改顺序、
    丢噪音），两条路就会分叉，而降级轮询的客户端会安静地看到不一样的东西。
    """
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            task_id = server.make_task("t-parity")
            server.inject(task_id, 12)

            async with await session() as sess:
                ws = await open_ws(sess, server, task_id, 0)
                await read_until_hello(ws)
                via_ws = await drain_events(ws, 12)
                await ws.close()

                via_rest: list[dict] = []
                cursor = 0
                while True:
                    _, body = await rest_events(sess, server, task_id, cursor, limit=5)
                    if not body["events"]:
                        break
                    via_rest += body["events"]
                    cursor = body["last_seq"]
                    if not body["has_more"]:
                        break

                ok(checks, "WS 与 REST 的 seq 序列逐条相同",
                   [e["seq"] for e in via_ws] == [e["seq"] for e in via_rest],
                   f"ws={len(via_ws)} rest={len(via_rest)}")
                ok(checks, "WS 与 REST 的事件内容逐条相同（含 payload）",
                   [json.dumps(e, sort_keys=True) for e in via_ws]
                   == [json.dumps(e, sort_keys=True) for e in via_rest])


# ----------------------------------------------------------------------
# 9. 终态必须进事件流
# ----------------------------------------------------------------------


async def phase_terminal_event(checks: Checks) -> None:
    """任务结束这件事，必须能从**事件流本身**看出来。

    这条验收要的是"重连后状态正确"。状态有两个去处：``tasks`` 表
    （权威）和事件流（叙事）。手机重连时两个都拿得到，所以只看表的那条路
    一直是对的——这也是这个洞藏得住的原因。但 ``ws.py`` 承诺了
    "客户端只靠 ``GET /api/tasks/{id}/events`` 轮询也能用"，那条路只有事件流：
    流里没有终点，轮询的客户端会**永远**以为任务还在跑。

    ``runner._finish`` 是**所有**终态写入的唯一出口，所以只在这里查一次。
    """
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            ctx = server.ctx

            # --- 情况一：适配器没发过终态事件（create_session 失败、send 抛异常、
            #     网关关停被取消——这三条适配器根本走不到）---
            task_id = server.make_task("t-term-1")
            ctx.runner._finish(task_id, TaskStatus.FAILED, "创建会话失败：找不到 claude")

            events = ctx.store.events_since(task_id, 0, limit=100)
            terminal = [e for e in events if e.type in (EventType.TASK_FAILED, EventType.TASK_COMPLETED)]
            ok(checks, "_finish 补上终态事件（适配器没发过时）",
               len(terminal) == 1, f"n={len(terminal)}")
            ok(checks, "补的终态事件带着原因，不是一条空事件",
               bool(terminal) and terminal[0].payload.get("reason"),
               f"{terminal[0].payload if terminal else None}")
            ok(checks, "补的事件标明是补的（和适配器自己发的区分开）",
               bool(terminal) and terminal[0].payload.get("recovered") is True,
               f"{terminal[0].payload if terminal else None}")

            # --- 情况二：适配器已经发过了，不能再补一条 ---
            # 两条终点会让客户端渲染出"完成"和"失败"两个结尾
            task_id = server.make_task("t-term-2")
            server.inject(task_id, 1)
            ctx.runner._sink(task_id)(
                Event(task_id=task_id, type=EventType.TASK_COMPLETED, payload={"reason": "success"})
            )
            ctx.runner._finish(task_id, TaskStatus.COMPLETED, None, exit_reason="success")

            events = ctx.store.events_since(task_id, 0, limit=100)
            terminal = [e for e in events if e.type in (EventType.TASK_FAILED, EventType.TASK_COMPLETED)]
            ok(checks, "适配器已发过终态事件时不重复补（事件流只有一个终点）",
               len(terminal) == 1, f"n={len(terminal)}")

            # --- 情况三：非终态不该发终态事件 ---
            task_id = server.make_task("t-term-3")
            ctx.runner._finish(task_id, TaskStatus.RUNNING)
            events = ctx.store.events_since(task_id, 0, limit=100)
            ok(checks, "非终态不产生终态事件", events == [], f"n={len(events)}")

            # --- 情况四：轮询客户端能靠事件流本身看出任务结束了 ---
            # 这是这一段的**整个目的**：不查 tasks 表也能知道。
            task_id = server.make_task("t-term-4")
            ctx.runner._finish(task_id, TaskStatus.DISCONNECTED, "网关重启，任务被标记为断开",
                               exit_reason="gateway_restart")
            async with await session() as sess:
                _, body = await rest_events(sess, server, task_id, 0)
                kinds = [e["type"] for e in body["events"]]
                ok(checks, "只轮询事件流也能看到任务结束（不必查 tasks 表）",
                   "task_failed" in kinds, f"{kinds}")

            # --- 情况五：recover() 那条老路径不能因为这次改动变成两条 ---
            task_id = server.make_task("t-term-5")
            ctx.store.update_task(task_id, status=TaskStatus.RUNNING, started_at=1.0)
            recovered = ctx.runner.recover()
            ok(checks, "recover 仍能认出遗留任务", any(t.id == task_id for t in recovered),
               f"{[t.id for t in recovered]}")
            events = ctx.store.events_since(task_id, 0, limit=100)
            terminal = [e for e in events if e.type in (EventType.TASK_FAILED, EventType.TASK_COMPLETED)]
            ok(checks, "recover 的路径也只产生一个终点（没被改成两条）",
               len(terminal) == 1, f"n={len(terminal)}")


# ----------------------------------------------------------------------
# 10. 参数错误要说明原因，不能静默挂起
# ----------------------------------------------------------------------


async def phase_bad_params(checks: Checks) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            async with await session() as sess:
                ws = await sess.ws_connect(
                    f"{server.base}/ws?task_id=t-nope",
                    headers={"X-Token": HOST_TOKEN},
                    timeout=10,
                )
                msg = await ws.receive(timeout=10)
                frame = json.loads(msg.data) if msg.type is WSMsgType.TEXT else {}
                ok(checks, "未知 task_id：先给出 error 帧说明原因",
                   frame.get("type") == "error" and "t-nope" in frame.get("error", ""),
                   f"{frame}")

                # 再收到关帧，且是 1008（policy violation）而不是静默超时
                msg = await ws.receive(timeout=10)
                ok(checks, "未知 task_id：随后以 1008 关闭",
                   msg.type is WSMsgType.CLOSE and msg.data == 1008, f"{msg.type} {msg.data}")

                # 没带 task_id 也一样，且原因要说清楚"未指定"
                ws = await sess.ws_connect(
                    f"{server.base}/ws", headers={"X-Token": HOST_TOKEN}, timeout=10
                )
                msg = await ws.receive(timeout=10)
                frame = json.loads(msg.data) if msg.type is WSMsgType.TEXT else {}
                ok(checks, "缺 task_id：原因文案指出「未指定」",
                   frame.get("type") == "error" and "未指定" in frame.get("error", ""),
                   f"{frame}")

                # 无凭证：中间件在握手前就挡住
                try:
                    ws = await sess.ws_connect(f"{server.base}/ws?task_id=x", timeout=10)
                    first = await ws.receive(timeout=5)
                    rejected = first.type in (WSMsgType.CLOSE, WSMsgType.ERROR)
                except Exception:  # noqa: BLE001 —— 握手被拒也算通过
                    rejected = True
                ok(checks, "无凭证订阅被拒", rejected)

                # 非法 since 退回 0 而不是报错
                task_id = server.make_task("t-badsince")
                server.inject(task_id, 2)
                ws = await open_ws(sess, server, task_id, 0)
                await ws.close()
                ws = await sess.ws_connect(
                    f"{server.base}/ws?task_id={task_id}&since=abc",
                    headers={"X-Token": HOST_TOKEN}, timeout=10,
                )
                hello = await read_until_hello(ws)
                ok(checks, "非法 since 退回 0（不是报错，也不是丢事件）",
                   hello["since"] == 0, f"since={hello['since']}")
                got = await drain_events(ws, 2)
                ok(checks, "非法 since 之后仍能拿到全部事件", len(got) == 2, f"n={len(got)}")
                await ws.close()


# ----------------------------------------------------------------------
# 11. ping / pong：前端探活依赖的那一半，在服务端这边
# ----------------------------------------------------------------------


async def phase_ping_pong(checks: Checks) -> None:
    """服务端对 ``{"type": "ping"}`` 必须回 ``{"type": "pong"}``。

    单看服务端，这一对是可有可无的（连接断了 ``onclose`` 会告诉前端）。
    但前端的**探活**完全建在它上面：手机进隧道时连接是被静默丢包的，
    没有 ``onclose`` 可等，只有"发 ping 等 pong"能发现。
    前端那边一旦判死就换连接，所以**这里漏一次 pong 的后果不是"少一个
    pong"，而是每个空闲的任务页都会周期性地误判重连**。

    前端的逻辑本身在 ``probes/verify_liveness.mjs`` 里单独验（假 WebSocket、
    假时钟，零耗时）；这里验的是它对面的那个真实实现。
    """
    with tempfile.TemporaryDirectory() as tmp:
        async with Server(Path(tmp)) as server:
            async with await session() as sess:
                task_id = server.make_task("t-ping")
                server.inject(task_id, 1)
                ws = await open_ws(sess, server, task_id, 0)
                await read_until_hello(ws)
                await drain_events(ws, 1)

                await ws.send_str(json.dumps({"type": "ping"}))
                frame = await _next_frame_of_type(ws, "pong", timeout=5)
                ok(checks, "服务端对 ping 回 pong", frame is not None, f"{frame}")

                # 连着问，不能只答第一次（前端是每 10 秒问一次的）
                pongs = 0
                for _ in range(3):
                    await ws.send_str(json.dumps({"type": "ping"}))
                    if await _next_frame_of_type(ws, "pong", timeout=5) is not None:
                        pongs += 1
                ok(checks, "反复 ping 每次都回", pongs == 3, f"{pongs}/3")

                # 前端可能在重连补发的中途插一个 ping 进来，两条路不能互相打断
                server.inject(task_id, 3)
                await ws.send_str(json.dumps({"type": "ping"}))
                got = await drain_events(ws, 3)
                ok(checks, "补发事件和 pong 交错也不丢事件", len(got) == 3, f"n={len(got)}")

                # 垃圾帧不能让连接断掉：前端出 bug 发了个没用的东西时，
                # 症状必须是"没反应"，不是"连接被踢掉然后无限重连"。
                await ws.send_str("这不是 JSON")
                await ws.send_str(json.dumps({"type": "谁"}))
                await ws.send_str(json.dumps(["不是对象"]))
                await ws.send_str(json.dumps({"type": "ping"}))
                frame = await _next_frame_of_type(ws, "pong", timeout=5)
                ok(checks, "收到垃圾帧后连接照常活着（只忽略，不断开）", frame is not None, f"{frame}")

                await ws.close()


async def _next_frame_of_type(ws, type_: str, timeout: float = 5.0) -> dict | None:
    """等下一条指定类型的帧；期间来的 event / task 都不要。

    收不满就返回 None 而不是抛异常——"没等到 pong"本身就是要断言的结论。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            return None
        try:
            msg = await ws.receive(timeout=left)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        if msg.type is not WSMsgType.TEXT:
            return None
        frame = json.loads(msg.data)
        if frame.get("type") == type_:
            return frame


# ----------------------------------------------------------------------


async def main() -> int:
    checks: Checks = []
    for phase in (
        phase_rest_replay,
        phase_live,
        phase_gap,
        phase_slow_consumer,
        phase_batch,
        phase_multi_subscriber,
        phase_status,
        phase_ws_equals_rest,
        phase_terminal_event,
        phase_bad_params,
        phase_ping_pong,
    ):
        try:
            await phase(checks)
        except Exception as exc:  # noqa: BLE001 —— 一段崩了不该掩盖其它段
            import traceback
            checks.append((f"{phase.__name__} 抛异常", False, f"{type(exc).__name__}: {exc}"))
            if VERBOSE:
                traceback.print_exc()

    passed = sum(1 for _, p, _ in checks if p)
    print("=" * 72)
    for label, p, detail in checks:
        mark = "✓" if p else "✗"
        line = f"  {mark} {label}"
        if not p or VERBOSE:
            line += f"   [{detail}]" if detail else ""
        print(line)
    print("=" * 72)
    print(f"  {passed}/{len(checks)} 项通过")
    if passed != len(checks):
        print("\n  未通过：")
        for label, p, detail in checks:
            if not p:
                print(f"    ✗ {label}   [{detail}]")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
