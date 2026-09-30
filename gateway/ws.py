"""WebSocket：把某个任务的事件实时推给客户端，并支持按 seq 补发。

只有一条规则，但它决定了其他所有细节：

    **库是唯一事实来源，事件总线只是"有事发生了"的唤醒信号。**

收到唤醒之后不是把队列里那条事件直接发出去，而是拿 ``last_seq`` 去库里查
``seq > last_seq`` 的新事件——推的是查询结果，不是那条唤醒。换来三件事：

  1. **断线重连和实时推送走的是同一段代码**（都是这个查询），
     不会出现"补发的逻辑改了、实时的忘了改"这种分叉
  2. 总线为慢消费者丢弃事件时（队列满丢最旧，见 :class:`~gateway.events.EventBus`）
     **不会静默丢数据**——下一次唤醒会一并查回来。手机进隧道那几十秒正好撞上这个场景
  3. 重复唤醒只是多查一次空结果，天然幂等，没有去重逻辑要维护

代价是每条事件多一次索引查询。本地 SQLite 是微秒级，完全划算。

顺带得到一个好性质：**WebSocket 只是优化，不是必需**。客户端只靠
``GET /api/tasks/{id}/events?since=N`` 轮询也能得到完全一样的事件序列。

协议（双向都是 JSON 文本帧）：

    服务端 → 客户端
        {"type": "hello", "task": {...}, "since": N}   连接就绪，N 是本次补发的起点
        {"type": "event", "event": {...}}              一条归一化事件
        {"type": "task",  "task": {...}}               任务状态变了（只在变时发）
        {"type": "pong"}
        {"type": "error", "error": "..."}              随后即关闭

    客户端 → 服务端
        {"type": "ping"}                               探活用；重连靠 URL 上的 since，不靠指令

ping/pong 的两头，各管一半，缺一不可：

  - ``WS_HEARTBEAT`` 是**协议层**的 ping，浏览器自动回 pong。它管的是
    "这条 TCP 连接还通不通"，**JS 完全看不见**；而且方向是服务端探客户端，
    探不出"客户端发的包到不了服务端"。
  - ``{"type": "ping"}`` → ``{"type": "pong"}`` 是给客户端**主动探活**用的，
    另一头在 ``web/app.js`` 的 ``openStream`` 里（间隔常量也在那儿）：
    定期问一次，连着几次没回音就判这条连接死了，**直接另起一条**，
    不等 ``onclose``。

为什么非要有主动探活：连接被**明确关闭**（RST、进程退出）时浏览器会触发
``onclose``，一切照常；但连接**被黑洞掉**（手机进隧道、Wi-Fi 转蜂窝时
静默丢包）时 ``onclose`` 要等几分钟的系统超时，这期间界面一直写着"已连接"
而事件流是静止的——比报错更糟，因为用户不知道该刷新。

这一对是有代价的：判早了会让闲着的任务页反复重连（手机上大部分时间
任务页就是闲着的，服务端一帧都不发）。所以前端那边对**空闲**和
**切后台**两种情形都专门压过，两个方向都有测试兜着：
服务端这半在 ``probes/verify_reconnect.py``，前端那半在
``probes/verify_liveness.mjs`` 与 ``probes/verify_web.py`` 第 6 节。
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

from .api import dumps_json
from .context import get_ctx

log = logging.getLogger("gateway.ws")

routes = web.RouteTableDef()


async def _send(ws: web.WebSocketResponse, frame: dict) -> None:
    """发一帧。

    统一走 ``dumps_json``：``ws.send_json`` 用的是 aiohttp 自己的 ``json.dumps``，
    默认 ``ensure_ascii=True``——任务失败原因、工具名里的中文全变成 ``\\u62a5\\u9519``，
    而手机上读事件流正是为了看这些字。
    """
    await ws.send_json(frame, dumps=dumps_json)

#: 服务端 ping 的间隔。浏览器收到 ping 会在协议层自动回 pong，JS 看不到。
WS_HEARTBEAT = 30.0

# 注意 `WebSocketResponse` 的 ``compress`` 默认是 True，也就是协商了
# permessage-deflate：**文本帧在线上是压过的**。手机上这是好事（事件帧
# 动辄几百字节，压缩省流量），但会让"抓包认帧"这种办法失效——
# ``{"type":"ping"}`` 靠 LZ77 窗口压到只剩几个字节，线上根本没有那个
# 字面量。所以验探活要在页面里数（那儿看到的是解压后的文本），
# 不要在传输层按子串找。踩过一次，见 probes/verify_web.py 第 6c 节。

#: 空闲时多久空转一次。**必须有这个上限**：不然连接悄悄断了（手机换网、
#: 拔网线）而任务恰好也不再产生事件时，这个协程会永远挂在队列上，
#: 订阅者只增不减。
WS_TICK = 15.0

#: 单次补发的上限。断线一小时再连回来可能有几千条，一次全推会撑爆手机内存。
#: 超过了就在同一个循环里分几次推——每次推完立刻再查一轮，不等唤醒。
WS_BATCH = 500

#: 1008 = policy violation。
WS_CLOSE_REJECTED = 1008


@routes.get("/ws")
async def task_stream(request: web.Request) -> web.WebSocketResponse:
    """订阅一个任务的事件流。

    查询参数：
        task_id  必填
        since    从 seq 大于它的位置开始补发（默认 0 = 从头）
    """
    ctx = get_ctx(request)
    ws = web.WebSocketResponse(heartbeat=WS_HEARTBEAT)
    await ws.prepare(request)

    task_id = (request.query.get("task_id") or "").strip()
    task = ctx.store.get_task(task_id) if task_id else None
    if task is None:
        # 先说明原因再关，否则客户端只看到代码 1008，无从判断是参数错了还是 token 过期了
        await _send(
            ws, {"type": "error", "error": f"没有这个任务：{task_id or '（未指定 task_id）'}"}
        )
        await ws.close(code=WS_CLOSE_REJECTED, message=b"unknown task")
        return ws

    since = _non_negative_int(request.query.get("since"))
    last_seq = since
    last_status = task.status
    queue = ctx.bus.subscribe()

    # 必须有人持续调 receive()，否则 aiohttp 读不到 pong，会用自己的心跳
    # 把这条连接判死（autoping 的处理挂在 receive() 里）。这不是可选项。
    reader = asyncio.create_task(_drain_client(ws, queue))

    async def flush() -> None:
        """把库里 seq > last_seq 的事件推完。"""
        nonlocal last_seq
        while True:
            events = ctx.store.events_since(task_id, last_seq, limit=WS_BATCH)
            if not events:
                return
            for event in events:
                await _send(ws, {"type": "event", "event": event.model_dump(mode="json")})
                last_seq = event.seq
            if len(events) < WS_BATCH:
                return

    try:
        await _send(ws, {"type": "hello", "task": task.model_dump(mode="json"), "since": since})

        while not ws.closed:
            try:
                await flush()

                # 任务状态不在事件里（它在 tasks 表上），单独推一次。
                # 只在变化时推，所以这点查询一秒钟最多几次。
                fresh = ctx.store.get_task(task_id)
                if fresh is not None and fresh.status is not last_status:
                    last_status = fresh.status
                    await _send(ws, {"type": "task", "task": fresh.model_dump(mode="json")})
            except (ConnectionResetError, RuntimeError) as exc:
                # 对面走了。任务终态之后也保持连接（不再有事件，空转成本接近零），
                # 由客户端决定什么时候关——服务端主动断开会让前端以为"出问题了"。
                log.debug("任务 %s 的订阅连接已断开：%s", task_id, exc)
                break

            try:
                item = await asyncio.wait_for(queue.get(), timeout=WS_TICK)
            except (TimeoutError, asyncio.TimeoutError):
                pass  # 空转一次，回头检查连接还在不在
            else:
                # ``None`` 是 _drain_client 在退出时放的"客户端走了"信号。
                # 收到它就直接结束，不必再走一轮 flush——对面已经没人收了。
                if item is None:
                    break
    finally:
        ctx.bus.unsubscribe(queue)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    return ws


async def _drain_client(ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
    """消费客户端发来的帧。

    目前只认 ``{"type": "ping"}`` → ``{"type": "pong"}``，给客户端一个
    主动探活的手段（手机切 Wi-Fi/蜂窝时，浏览器不会主动告诉你连接断了）。

    这里和主循环都会往同一个 socket 写。aiohttp 的 WebSocketWriter 内部有
    发送锁，两个协程并发 send 不会把帧写串。

    **退出时要往队列里丢一个 ``None``。** 主循环只在"有事件"或"空转超时"
    两种情况醒来，所以没有这个信号的话，客户端断开后主循环要等满一个
    ``WS_TICK``（15 秒）才发现。那 15 秒里：订阅者还挂在总线上（事件照发、
    队列照攒），``flush()`` 还在往一个死 socket 写，而手机那边已经重新连上
    新的一条了——于是同一个客户端挂着两条订阅。手机在电梯口反复断连时会叠起来。
    """
    try:
        async for msg in ws:
            if msg.type is WSMsgType.ERROR:
                break
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and data.get("type") == "ping":
                await _send(ws, {"type": "pong"})
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        # 队列可能已满（慢消费者正好赶上），这时丢掉最旧的换取这个信号的位置：
        # **让主循环及时收摊，比多留一条事件更重要**——事件本来就在库里，
        # 客户端重连时按 seq 补发，一条都不会少。
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(None)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


def _non_negative_int(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0
