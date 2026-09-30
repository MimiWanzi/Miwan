#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 5 层起、**在真实浏览器里**把前端点一遍的验收。

为什么不满足于"用 HTTP 断言接口都对"：第 4 层已经那么验过了，而前端出错的
地方恰恰在接口与 DOM 之间——路径写错、字段名拼错、事件没渲染、hash 路由不跳转。
这些只有真的把页面跑起来才看得见。后来每一层都往这里加了一节：
第 6 层的绊线渲染、第 7 层的配对与设备、第 8 层的断线重连。

第 6 节因此长得分成了三段，验的是**三种不同的坏法**：
关掉页面重开（since=0 全量补发）、连接被掐断（RST）、连接被黑洞掉
（静默丢包）。最后一种只有前端的主动探活能发现，为此实测里专门有一个
会丢包但不掐线的 TCP 中继（:class:`Relay`）。

做法：起网关 → 起一个 **headless Chrome** → 用 DevTools Protocol 连上去
→ 在页面里执行 JS 做断言。不装任何东西：Chrome 本来就在，CDP 走 WebSocket，
而 aiohttp 自带 WebSocket 客户端。

会真实调用**三次** Claude（一次读文件、一次断线重连、一次用来中断），
消耗少量额度。临时库，不碰 data/ 下的真库；但每次运行都会在
~/.claude/projects/ 留下会话文件。

跑法：
    python probes/verify_web.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import load_config  # noqa: E402
from gateway.context import CTX  # noqa: E402
from gateway.events import Event, EventType  # noqa: E402
from gateway.models import Task, TaskStatus  # noqa: E402
from gateway.server import build_app  # noqa: E402

TOKEN = "web-verify-token"
DEBUG_PORT = 9333

READ_PROMPT = "读一下 main.py，用一句话说明它做什么。不要修改任何文件。"

SLOW_PROMPT = (
    "用 Glob 列出 gateway 目录下所有的 .py 文件，然后逐个 Read，"
    "最后用一句话说明每个模块负责什么。不要修改任何文件。"
)

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_browser() -> str | None:
    for path in CHROME_CANDIDATES:
        if Path(path).is_file():
            return path
    return None


# ----------------------------------------------------------------------
# 极简 CDP 客户端
# ----------------------------------------------------------------------


class CDP:
    """够用就好的 DevTools Protocol 客户端。

    ``send`` 会顺手把不请自来的事件（没有 id 的那些）收起来——其中
    ``Runtime.exceptionThrown`` 是重点：页面里的 JS 一旦抛异常，界面上
    往往只是"某某东西没出现"，而**真正的原因只在控制台里**。
    把它抓下来，失败时才有话可说。
    """

    def __init__(self, ws) -> None:
        self.ws = ws
        self._next = 0
        self.exceptions: list[str] = []

    async def _pump(self, want: int) -> dict:
        while True:
            msg = await self.ws.receive_json(timeout=60)
            if msg.get("id") == want:
                return msg
            if msg.get("method") == "Runtime.exceptionThrown":
                details = msg["params"]["exceptionDetails"]
                text = (details.get("exception") or {}).get("description") or details.get("text")
                self.exceptions.append(str(text).split("\n")[0])

    async def send(self, method: str, **params) -> dict:
        self._next += 1
        mid = self._next
        await self.ws.send_json({"id": mid, "method": method, "params": params})
        msg = await self._pump(mid)
        if "error" in msg:
            raise RuntimeError(f"CDP {method} 失败：{msg['error']}")
        return msg.get("result", {})

    async def js(self, expression: str):
        result = await self.send(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=True,
            returnByValue=True,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(f"页面里求值出错：{result['exceptionDetails'].get('text')}")
        return result["result"].get("value")

    async def wait_for(self, expression: str, timeout: float = 60.0, interval: float = 0.2):
        """轮询直到表达式为真。断言类的东西都写成"等它出现"，
        因为前端是异步的——固定 sleep 要么太慢要么不够。"""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = await self.js(expression)
            if last:
                return last
            await asyncio.sleep(interval)
        raise TimeoutError(f"等了 {timeout:g}s 也没等到：{expression}\n（最后一次结果 {last!r}）")


# ----------------------------------------------------------------------


class Relay:
    """一个透明 TCP 中继，站在浏览器和网关之间。

    **存在的唯一理由：让测试能把浏览器的连接掐断，而网关不受影响。**

    本来想用 CDP 的 ``Network.emulateNetworkConditions(offline=True)`` 来模拟
    "手机没信号了"，实测**不行**：它不会关闭一条已经建立的 WebSocket
    （连 127.0.0.1 尤其不会），前端的 ``onclose`` 永远不触发，
    于是"退避重连"那段代码一次都走不到——测试会卡在等 ``#conn`` 变 off。

    也不好在页面里直接把 WebSocket 关掉：``app.js`` 是 ``type="module"``，
    ``openStream`` 不在 window 上，测试够不着那个对象。为测试往生产代码里
    挂一个全局句柄是不划算的交换。

    所以让浏览器连中继、中继连网关。掐中继 = 掐浏览器，网关那边任务照跑。
    改了端口不影响 cookie：**cookie 按主机存，不按端口**。
    """

    def __init__(self, upstream_port: int) -> None:
        self.upstream_port = upstream_port
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._conns: set[tuple] = set()
        self._blackhole = False

    async def start(self) -> "Relay":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        except OSError:
            cw.close()
            return
        self._conns.add((cr, cw, ur, uw))
        try:
            await asyncio.gather(self._pipe(cr, uw), self._pipe(ur, cw))
        except Exception:  # noqa: BLE001 —— 掐断是预期内的，不是错误
            pass
        finally:
            self._conns.discard((cr, cw, ur, uw))
            for w in (cw, uw):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _pipe(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        """原样转发一个方向。

        **刻意不在这里认帧。** 曾经按子串数过 ping / pong，想用它证明
        "探活这一对真的在跑"，数出来永远是 0——因为 aiohttp 的
        ``WebSocketResponse`` 默认 ``compress=True``，浏览器又总会提
        permessage-deflate，文本帧是**压过的**：``{"type":"ping"}`` 靠
        LZ77 窗口压到只剩几个字节，字面量根本不在线上。
        而那个 0 特别容易被读成"探活没在跑"，把人引到错的地方去查
        （确实引偏了一轮）。要验这件事就在**页面里**数——那儿看到的
        是解压后的文本，见第 6c 节的 ``__pings`` / ``__pongs``。
        """
        while True:
            data = await r.read(65536)
            if not data:
                return
            if self._blackhole:
                continue  # 读掉，但不转发
            w.write(data)
            await w.drain()

    def blackhole(self, on: bool) -> None:
        """静默丢包：连接**不断**，但字节一个都不过去。

        和 :meth:`cut` 是两种不同的坏法，前端要靠两套不同的机制才能发现：

        - **掐断**（RST）→ 浏览器立刻 onclose → 退避重连。``cut`` 验的就是这条。
        - **黑洞**（静默丢包）→ 什么都不发生。两端 TCP 都以为对方还在，
          没有 onclose 会触发。只有**主动探活**能发现。手机进隧道、
          Wi-Fi 转蜂窝的那一两秒就是这个样子，而且它是**最常见**的那种断，
          不是掐断。

        实现上刻意是"读掉但不转发"，不是"不读"：真不读的话 TCP 接收窗口
        很快填满，发送方会阻塞、重传，几分钟后自己超时断开——那就又变成
        掐断了，测不到"连接活着但字节不通"这个状态。
        """
        self._blackhole = on

    def cut(self) -> int:
        """掐断所有在途连接——模拟手机没信号。

        用 ``abort()`` 而不是 ``close()``：close 是优雅关闭（发 FIN），
        而断网的真实表现是连接**断掉**。RST 让浏览器立刻报错并触发 onclose，
        不用等 TCP 重传超时（那个可能要几分钟，测试等不起）。
        """
        n = len(self._conns)
        for _, cw, _, uw in list(self._conns):
            for w in (cw, uw):
                try:
                    w.transport.abort()
                except Exception:  # noqa: BLE001
                    pass
        self._conns.clear()
        return n


def wait_for_debug_port(proc: subprocess.Popen, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{DEBUG_PORT}/json/list"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"浏览器起来就退了，退出码 {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                targets = json.load(resp)
            for target in targets:
                if target.get("type") == "page":
                    return target
        except Exception:  # noqa: BLE001 —— 端口还没起来是正常情况，继续等
            pass
        time.sleep(0.3)
    raise RuntimeError("浏览器没在 30s 内开出调试端口")


async def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        checks.append((label, bool(passed), detail))

    browser = find_browser()
    if browser is None:
        print("找不到 Chrome / Edge，无法做浏览器验收。")
        return 2
    print(f"浏览器：{browser}")

    chrome = None
    profile = tempfile.mkdtemp(prefix="ra-chrome-")

    with tempfile.TemporaryDirectory(prefix="ra-web-") as tmp:
        config = replace(load_config(), data_dir=Path(tmp), token=TOKEN, port=0)
        app = build_app(config)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}"
        print(f"网关起在 {base}")

        # 中继：第 6 节要能掐断浏览器的连接而留着网关（见 Relay 的注释）
        relay = await Relay(port).start()
        print(f"中继起在 {relay.base} → {base}")

        try:
            # 先把项目扫进临时库——浏览器一打开就会要 /api/projects
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/api/projects/refresh?token={TOKEN}") as resp:
                    payload = await resp.json()
            projects = payload["projects"]
            project = next(p for p in projects if p["name"] == "RemoteAgent")
            print(f"临时库备好：{len(projects)} 个项目")

            chrome = subprocess.Popen(
                [
                    browser,
                    "--headless=new",
                    f"--remote-debugging-port={DEBUG_PORT}",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    # 新版 Chromium 默认拒绝带 Origin 的 CDP 连接；放行本机调试
                    "--remote-allow-origins=*",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            target = wait_for_debug_port(chrome)
            print("浏览器已在调试端口上")

            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                    cdp = CDP(ws)
                    await cdp.send("Page.enable")
                    await cdp.send("Runtime.enable")

                    # ---- 1. 打开首页，项目列表要出来 ----
                    # 这条同时验了 cookie 引导：页面自己发的 fetch 必须成功，
                    # 而地址栏里带的 ?token= 只对"打开页面"这一次有效。
                    await cdp.send("Page.navigate", url=f"{base}/?token={TOKEN}#/")
                    await cdp.wait_for("document.querySelectorAll('#view .card').length > 0", 40)
                    names = await cdp.js(
                        "[...document.querySelectorAll('#view .card .name')].map(n => n.textContent)"
                    )
                    check("首页把项目列表渲染出来了", len(names) > 0, f"{len(names)} 个：{names[:3]}")
                    check("列表里有 RemoteAgent", "RemoteAgent" in names, str(names[:5]))

                    # ---- 2. 进项目页 ----
                    await cdp.js(f"location.hash = '#/p/{project['id']}'")
                    await cdp.wait_for("!!document.querySelector('.new-task textarea')", 20)
                    check("项目页出现了新建任务表单", True)
                    has_sessions = await cdp.js(
                        "document.querySelector('#view').textContent.includes('历史会话')"
                    )
                    check("项目页列出了历史会话区块", bool(has_sessions))

                    # ---- 3. 提交一个只读任务，应自动跳到任务页 ----
                    await cdp.js(
                        "(() => {"
                        f"  document.querySelector('.new-task textarea').value = {json.dumps(READ_PROMPT)};"
                        "  document.querySelector('.new-task').requestSubmit();"
                        "  return true;"
                        "})()"
                    )
                    await cdp.wait_for("location.hash.startsWith('#/t/')", 25)
                    task_hash = await cdp.js("location.hash")
                    check("提交任务后自动跳到任务页", str(task_hash).startswith("#/t/"), str(task_hash))

                    # ---- 4. 事件流实时渲染 ----
                    await cdp.wait_for("document.querySelectorAll('#view .stream .ev').length > 0", 60)
                    check("事件流开始渲染", True)
                    await cdp.wait_for("!!document.querySelector('#view .call')", 90)
                    check("看得到 Agent 的工具调用卡片", True)
                    tool_name = await cdp.js(
                        "document.querySelector('#view .call .tool').textContent"
                    )
                    check("工具卡片上显示了工具名", bool(tool_name), str(tool_name))
                    conn = await cdp.js("document.querySelector('#conn').className")
                    check("连接状态显示为已连接", conn == "on", f"class={conn!r}")

                    await cdp.wait_for(
                        "document.querySelector('#view').textContent.includes('✓ 完成')", 240
                    )
                    check("任务跑完，界面显示了完成标记", True)

                    # ---- 5. 关掉页面重开：事件必须一条不少地补回来 ----
                    # 这是原型书第 21 节的核心验收点，也是"库是唯一事实来源"
                    # 换来最直接的好处：前端不需要为此存任何状态。
                    # 快照整个事件流的文本，重开之后要求**逐字一致**。
                    # 只比条数是不够的：`>=` 连"重发了两遍"都拦不住，
                    # 而这里真正要证明的是"库里的东西被原样补发回来了"。
                    snapshot_js = (
                        "[...document.querySelectorAll('#view .stream .ev')]"
                        ".map(e => e.textContent).join('|||')"
                    )
                    before = await cdp.js(snapshot_js)
                    count = await cdp.js("document.querySelectorAll('#view .stream .ev').length")
                    check("重开前事件流非空", bool(before) and count > 0, f"{count} 条")

                    await cdp.send("Page.navigate", url=f"{base}/?token={TOKEN}{task_hash}")
                    await cdp.wait_for(
                        f"document.querySelectorAll('#view .stream .ev').length >= {count}", 60
                    )
                    after = await cdp.js(snapshot_js)
                    replayed = await cdp.js("document.querySelectorAll('#view .stream .ev').length")
                    check(
                        "重新打开页面后事件从库里逐字补发回来",
                        after == before,
                        f"重开前 {count} 条 / 重开后 {replayed} 条；内容{'一致' if after == before else '不一致'}",
                    )
                    check("重开后仍然看得到工具调用", bool(await cdp.js("!!document.querySelector('#view .call')")))

                    # ---- 6. 断线重连：任务在服务端继续跑 ----
                    # 原型书第 21 节的要求展开是两句：**任务不因为手机走了而停**，
                    # **回来之后能接上**。第 5 节验的是"关掉页面重开"
                    # （since=0 全量补发），那只是其中一条路。这里验另一条：
                    # 连接断在半途、任务还在产事件、客户端带着游标回来。
                    # 两条路在 openStream 里是两个分支，只测一条会漏掉
                    # 那段退避重连逻辑和那行按 seq 去重。
                    #
                    # 浏览器从这一节起改走**中继**，好让测试能把连接真的掐断
                    # 而网关不受影响（见 Relay 的注释：CDP 的 offline 模拟
                    # 关不掉一条已建立的 WebSocket）。cookie 按主机存不按端口，
                    # 所以换了端口照样带着凭证。
                    await cdp.send("Page.navigate", url=f"{relay.base}/?token={TOKEN}#/p/{project['id']}")
                    await cdp.wait_for("!!document.querySelector('.new-task textarea')", 30)
                    await cdp.js(
                        "(() => {"
                        f"  document.querySelector('.new-task textarea').value = {json.dumps(SLOW_PROMPT)};"
                        "  document.querySelector('.new-task').requestSubmit();"
                        "  return true;"
                        "})()"
                    )
                    await cdp.wait_for("location.hash.startsWith('#/t/')", 25)
                    reconn_hash = str(await cdp.js("location.hash"))
                    reconn_id = reconn_hash.split("/")[-1]
                    await cdp.wait_for("!!document.querySelector('#view .call')", 90)
                    check("断线测试用的任务已经跑起来", bool(reconn_id), reconn_id)

                    store = app[CTX].store
                    mark = store.event_count(reconn_id)

                    # 连接状态是从 on → off → on 一闪而过的（退避重连第一档只有 500ms），
                    # 靠 200ms 轮询去"抓到 off"是个会偶发失败的测试。
                    # 装个观察器把变化记下来，回头查日志——断言的是
                    # "确实断过又接上了"，而不是"我恰好看见了"。
                    await cdp.js(
                        "(() => {"
                        "  window.__connLog = [document.querySelector('#conn').className];"
                        "  new MutationObserver(() => window.__connLog"
                        "    .push(document.querySelector('#conn').className))"
                        "    .observe(document.querySelector('#conn'),"
                        "             {attributes: true, attributeFilter: ['class']});"
                        "  return true;"
                        "})()"
                    )

                    cut = relay.cut()
                    check("掐断浏览器到网关的连接（网关本身不动）", cut > 0, f"掐了 {cut} 条连接")

                    # **这一条才是原型书第 21 节的原文**："Task continues"。
                    # 手机这边彻底断了，任务必须在网关那边照跑。
                    # 这时候界面什么也收不到，所以不能看界面——看库。
                    grew = False
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        if store.event_count(reconn_id) > mark:
                            grew = True
                            break
                        await asyncio.sleep(0.5)
                    check(
                        "断线期间任务在服务端继续跑（事件照常落库）",
                        grew,
                        f"掐断那一刻 {mark} 条，"
                        + (f"之后涨到 {store.event_count(reconn_id)} 条" if grew
                           else "60 秒内没再增长（任务可能已经跑完了）"),
                    )

                    # 中继还在听，所以退避重连的下一次尝试应该立刻成功。
                    await cdp.wait_for("document.querySelector('#conn').className === 'on'", 90)
                    conn_log = await cdp.js("window.__connLog")
                    # 断了之后界面必须**自己发现**。发现不了的话，手机在电梯里
                    # 会一直显示"已连接"而什么都收不到——比报错更糟。
                    check(
                        "连接断掉后界面自己发现了（进过重连中）",
                        "off" in (conn_log or []),
                        f"状态变化序列 {conn_log}",
                    )
                    check(
                        "连接恢复后自动重连成功",
                        bool(conn_log) and conn_log[-1] == "on",
                        f"状态变化序列 {conn_log}",
                    )

                    # 等它跑完，让事件流静止——下面要拿"整页重载"当标尺比对。
                    await cdp.wait_for(
                        "document.querySelector('#view').textContent.includes('✓ 完成')", 300
                    )
                    after_reconnect = await cdp.js(snapshot_js)
                    reconn_n = await cdp.js("document.querySelectorAll('#view .stream .ev').length")

                    # 标尺：整页重载 = since=0 从库里全量补发，第 5 节已经证明它是对的。
                    # 断线重连拼出来的 DOM 必须和它**逐字相同**。
                    # 只比条数拦不住"重连时把历史又画了一遍"——那种情况条数只会更多，
                    # 看起来还挺"完整"。
                    await cdp.send("Page.navigate", url=f"{base}/?token={TOKEN}{reconn_hash}")
                    await cdp.wait_for(
                        f"document.querySelectorAll('#view .stream .ev').length >= {reconn_n}", 90
                    )
                    await cdp.wait_for(
                        "document.querySelector('#view').textContent.includes('✓ 完成')", 90
                    )
                    replayed = await cdp.js(snapshot_js)
                    reconn_m = await cdp.js("document.querySelectorAll('#view .stream .ev').length")
                    check(
                        "断线重连拼出来的事件流 = 全量补发的（无重无漏）",
                        replayed == after_reconnect,
                        f"重连后 {reconn_n} 条 / 全量补发 {reconn_m} 条；"
                        + ("内容一致" if replayed == after_reconnect else "内容不一致"),
                    )
                    check("重连回来之后事件流确实长过（不是空跑一趟）", reconn_n > 0, f"{reconn_n} 条")

                    # ---- 6b. 黑洞：连接活着，字节一个都不过去 ----
                    # 上面验的是**掐断**（RST → onclose → 退避重连）。那只是
                    # 一种断法，而且不是最常见的那种。手机进隧道、Wi-Fi 转蜂窝
                    # 时丢的那一两秒是**静默**的：TCP 两头都以为对方还在，
                    # onclose 要等系统的连接超时才来（几分钟）。没有探活的话，
                    # 界面会一直写着"已连接"而事件流是静止的——比报错更糟，
                    # 因为用户不知道该刷新。
                    #
                    # 用**已经跑完**的任务来验，是刻意的：探活跟任务在不在产事件
                    # 无关，要害恰恰是**空闲的连接也必须自己证明自己还活着**。
                    # 反过来说，这也意味着探活一旦写错，症状是空闲页面反复重连。
                    await cdp.send("Page.navigate", url=f"{relay.base}/?token={TOKEN}{reconn_hash}")
                    await cdp.wait_for("document.querySelector('#conn').className === 'on'", 60)

                    visibility = await cdp.js("document.visibilityState")
                    # 探活在页面切到后台时**故意**不判死（浏览器会节流定时器，
                    # 健康的连接也会长时间轮不到检查）。headless 里如果不是
                    # visible，这一节就不是在验探活，而是在验一件别的事——
                    # 与其让它悄悄通过，不如在这里明说。
                    check(
                        "页面在前台（否则这一节验的不是探活）",
                        visibility == "visible",
                        f"visibilityState={visibility!r}",
                    )

                    await cdp.js(
                        "(() => {"
                        "  window.__logBlack = [document.querySelector('#conn').className];"
                        "  new MutationObserver(() => window.__logBlack"
                        "    .push(document.querySelector('#conn').className))"
                        "    .observe(document.querySelector('#conn'),"
                        "             {attributes: true, attributeFilter: ['class']});"
                        "  return true;"
                        "})()"
                    )

                    # 不给任何提示：没有 RST、没有 FIN、没有 onclose。
                    # 前端只能靠"三次 ping 都没回音"自己发现（app.js 里的
                    # PING_MS / STALE_MS），这正是本节要验的东西。
                    relay.blackhole(True)
                    t0 = time.monotonic()
                    await cdp.wait_for("document.querySelector('#conn').className === 'off'", 120)
                    took = time.monotonic() - t0
                    black_log = await cdp.js("window.__logBlack")
                    check(
                        "静默丢包时界面自己发现断线（没有 onclose 可等）",
                        "off" in (black_log or []),
                        f"{took:.1f} 秒后变成 {black_log}",
                    )

                    # 恢复连通还不够：前端已经放弃那条旧连接、另起了一条，
                    # 而新连接的握手请求在黑洞期间被丢掉了，对面永远不会回应它。
                    # 这里用 cut 替它了断，好让"重连成功"这一段快点走完——
                    # 重连机制本身在上面已经验过了，这里不重复计入。
                    relay.blackhole(False)
                    relay.cut()
                    await cdp.wait_for("document.querySelector('#conn').className === 'on'", 120)
                    black_log = await cdp.js("window.__logBlack")
                    check(
                        "黑洞恢复后自动重连成功",
                        black_log == ["on", "off", "on"],
                        f"状态变化序列 {black_log}",
                    )

                    # 6c 用**自己的**记录数组，并且从这一刻开始计。
                    # 上一次这里是复用 `window.__connLog` 的，而它装着 6b 的
                    # ['on','off','on']——6c 于是把 6b 的战绩当成自己的
                    # 反复报同一个失败。两节各记各的，这种"验的是上一节"
                    # 就没法再发生。
                    await cdp.js(
                        "(() => {"
                        "  window.__logIdle = [document.querySelector('#conn').className];"
                        "  window.__evIdleAt = document.querySelectorAll('#view .stream .ev').length;"
                        "  new MutationObserver(() => window.__logIdle"
                        "    .push(document.querySelector('#conn').className))"
                        "    .observe(document.querySelector('#conn'),"
                        "             {attributes: true, attributeFilter: ['class']});"
                        "  return true;"
                        "})()"
                    )
                    # 探活到底有没有在跑，要在**页面里**数，不能从中继的字节里认：
                    # 文本帧是 permessage-deflate 压过的，``{"type":"ping"}`` 在
                    # 线上根本没有那个字面量（原因写在 ``Relay._pipe`` 上）。
                    # 页面里看到的是**解压后**的文本，这里数的才是"这一对真的在来回"。
                    #
                    # 用一个 ``send`` 的补丁去抓**当前那条** socket：不能等重连，
                    # 这一节要验的恰恰是"一直没重连"。定时器每 10 秒左右发一次
                    # ping，所以最多等十几秒就能抓到。
                    await cdp.js(
                        "(() => {"
                        "  if (window.__pingSpy) { return true; }"
                        "  window.__pingSpy = true;"
                        "  window.__pings = 0;"
                        "  window.__pongs = 0;"
                        "  window.__watched = false;"
                        "  const origSend = WebSocket.prototype.send;"
                        "  WebSocket.prototype.send = function (data) {"
                        "    if (typeof data === 'string' && data.includes('\"ping\"')) {"
                        "      window.__pings += 1;"
                        "      if (!window.__watched) {"
                        "        window.__watched = true;"
                        "        this.addEventListener('message', (e) => {"
                        "          if (typeof e.data === 'string' && e.data.includes('\"pong\"')) {"
                        "            window.__pongs += 1;"
                        "          }"
                        "        });"
                        "      }"
                        "    }"
                        "    return origSend.apply(this, arguments);"
                        "  };"
                        "  return true;"
                        "})()"
                    )
                    await cdp.wait_for("window.__pings >= 1", 25)

                    # ---- 6c. 反过来：空闲的健康连接**不能**被误判成断了 ----
                    # 这一条和第 6b 条是一对，缺一个都不行。探活的代价全落在
                    # 这里：判早了会让每个闲着没事的任务页每隔半分钟重连一次，
                    # 而手机上大部分时间任务页就是闲着的。
                    #
                    # 服务端在空闲任务上**一帧都不发**（协议层的 ping 浏览器
                    # 自动处理，JS 看不见），所以这一节里 lastRx 只能靠 pong
                    # 刷新——40 秒内状态一直是 on，就说明这对包确实在来回。
                    #
                    # 从"抓到第一条 ping"开始计：这样 40 秒窗口里的 pong
                    # 一个都不漏，两个数可以直接对起来。
                    pings0 = await cdp.js("window.__pings")
                    pongs0 = await cdp.js("window.__pongs")
                    t0 = time.monotonic()
                    await asyncio.sleep(40)
                    pings = (await cdp.js("window.__pings")) - pings0
                    pongs = (await cdp.js("window.__pongs")) - pongs0
                    idle_log = await cdp.js("window.__logIdle")
                    ev_now = await cdp.js("document.querySelectorAll('#view .stream .ev').length")
                    ev_then = await cdp.js("window.__evIdleAt")
                    check(
                        "空闲 40 秒不会被误判成断线",
                        idle_log == ["on"],
                        f"{time.monotonic() - t0:.0f} 秒内状态变化序列 {idle_log}",
                    )
                    # 上面那条只证明"界面没动"。界面没动也可能是因为探活整个
                    # 没在跑（定时器压根没起来），那样这一节就是空过——看着绿，
                    # 什么都没验到。所以要另外确认这一对真的在来回：
                    # 40 秒 ≈ 3~4 个周期，3 次是宽松下限。
                    check(
                        "这 40 秒里 ping/pong 真的在来回（不是探活没跑）",
                        pings >= 3 and pongs >= 3,
                        f"客户端发出 {pings} 次 ping、收到 {pongs} 次 pong",
                    )
                    check(
                        "这 40 秒里确实什么都没发生（不是别的东西在撑着连接）",
                        ev_now == ev_then,
                        f"事件数 {ev_then} → {ev_now}",
                    )

                    # 后面的小节回到直连，让它们的条件和加这一节之前完全一样。
                    await cdp.send("Page.navigate", url=f"{base}/?token={TOKEN}#/p/{project['id']}")
                    await cdp.wait_for("!!document.querySelector('.new-task textarea')", 30)

                    # ---- 7. 中断一个正在跑的任务 ----
                    await cdp.js(f"location.hash = '#/p/{project['id']}'")
                    await cdp.wait_for("!!document.querySelector('.new-task textarea')", 20)
                    await cdp.js(
                        "(() => {"
                        f"  document.querySelector('.new-task textarea').value = {json.dumps(SLOW_PROMPT)};"
                        "  document.querySelector('.new-task').requestSubmit();"
                        "  return true;"
                        "})()"
                    )
                    await cdp.wait_for("location.hash.startsWith('#/t/')", 25)
                    await cdp.wait_for("!!document.querySelector('#view .call')", 90)
                    running = await cdp.js(
                        "document.querySelector('#view .task-head .pill').textContent"
                    )
                    check("中断前任务确实在跑", running == "运行中", f"实际 {running!r}")

                    await cdp.js("document.querySelector('#view .task-head button.danger').click()")
                    await cdp.wait_for(
                        "document.querySelector('#view').textContent.includes('已中断')", 60
                    )
                    check("点中断后界面进入已中断", True)
                    disabled = await cdp.js(
                        "document.querySelector('#view .task-head button.danger').disabled"
                    )
                    check("中断按钮随之置灰", bool(disabled))

                    # ---- 8. 绊线事件在手机上的样子 ----
                    # 用**合成事件**而不是真去撞一次绊线：这里要验的是"渲染对不对"，
                    # 不是"拦截灵不灵"（那个由 verify_full_autonomy.py 花钱验）。
                    # 合成进去之后走的是同一条补发链路——since=0 从库里读，
                    # 和真实事件没有任何区别。所以这一节不花额度，却覆盖了真路径。
                    store = app[CTX].store
                    synth = "t-synth-perm"
                    store.create_task(
                        Task(
                            id=synth, project_id=project["id"], agent_id="claude_code",
                            prompt="合成：只为验证渲染", status=TaskStatus.COMPLETED,
                            created_at=time.time(),
                        )
                    )
                    for ev in (
                        Event(task_id=synth, type=EventType.TASK_STARTED,
                              payload={"prompt": "合成", "cwd": "D:\\x"}),
                        # 新格式：session 会把 source 改写成 tripwire 并补上原因
                        Event(task_id=synth, type=EventType.PERMISSION_REQUEST, payload={
                            "tool_name": "PowerShell",
                            "tool_input": {"command": "Remove-Item -Recurse -Force .\\victim"},
                            "decision": "deny", "source": "tripwire",
                            "reason": "递归或强制删除：删掉的目录进不了回收站",
                            "tripwire": {"id": "rm-recurse", "title": "递归或强制删除"},
                        }),
                        # 旧格式：第 6 层之前落的库，**必须仍然认**
                        Event(task_id=synth, type=EventType.PERMISSION_REQUEST, payload={
                            "tool_name": "Write", "tool_input": {"file_path": "a.txt"},
                            "decision": "deny", "source": "no_host",
                        }),
                    ):
                        store.append_event(ev)

                    await cdp.send("Page.navigate", url=f"{base}/?token={TOKEN}#/t/{synth}")
                    await cdp.wait_for(
                        "document.querySelectorAll('#view .ev.perm').length >= 2", 30
                    )
                    perms = await cdp.js(
                        "[...document.querySelectorAll('#view .ev.perm')].map(e => e.textContent)"
                    )
                    check("绊线事件渲染出来了", len(perms) >= 2, f"{len(perms)} 张卡片")
                    check(
                        "卡片写明了是哪条绊线（不是笼统的「被拒绝」）",
                        any("递归或强制删除" in str(t) for t in perms),
                        str(perms[0])[:160],
                    )
                    check(
                        "旧格式（no_host）的历史事件仍然能渲染，不是空白",
                        any("还没有权限通道" in str(t) for t in perms),
                        str(perms[-1])[:120],
                    )

                    # ---- 9. 第 7 层：配对界面 ----
                    # 前面一路都是带着 ?token= 进来的，浏览器手里已经有凭证了。
                    # 要验"没凭证会怎样"，必须先把 cookie 清干净——
                    # 否则这一节测到的是"已经有凭证会怎样"，而那是成功那条路径。
                    await cdp.send("Network.enable")
                    await cdp.send("Network.clearBrowserCookies")

                    await cdp.send("Page.navigate", url=f"{base}/")
                    await cdp.wait_for("!!document.querySelector('.pair-form')", 30)
                    check("没凭证打开页面 → 直接进配对界面", True)
                    # 这条是这一节的重点：401 是"该配对"，不是"加载失败"。
                    # 重试按钮在这里毫无意义，而且会把用户引向错误的排查方向。
                    check(
                        "没把凭证问题显示成「加载失败」",
                        not await cdp.js(
                            "document.querySelector('#view').textContent.includes('加载失败')"
                        ),
                    )
                    check(
                        "配对界面说明了码从哪来",
                        "横幅" in await cdp.js("document.querySelector('#view').textContent"),
                    )

                    # 输错码：要就地提示，而且**不能**把人从表单上赶走——
                    # 一旦离开表单，刚输入的一半内容就没了，用户会以为要用电脑重来
                    real_code = app[CTX].auth.issue_code()
                    wrong = "000000" if real_code != "000000" else "111111"
                    await cdp.js(
                        "(() => {"
                        f"  document.querySelector('.code-input').value = {json.dumps(wrong)};"
                        "  document.querySelector('.pair-form').requestSubmit();"
                        "  return true;"
                        "})()"
                    )
                    await cdp.wait_for("!document.querySelector('.pair-msg').hidden", 20)
                    message = await cdp.js("document.querySelector('.pair-msg').textContent")
                    check("配对码错了会就地提示", "配对码" in str(message), str(message))
                    check(
                        "配对失败后仍留在配对界面（没被弹走）",
                        await cdp.js("location.hash === '#/pair'"),
                        await cdp.js("location.hash"),
                    )

                    # 输对码。**故意带上横幅上那个空格**：用户就是照着屏幕抄的，
                    # 而服务端比的是 "123456"。这条要是挂了，说明"照着抄"这个
                    # 最自然的动作会稳定失败。
                    spaced = f"{real_code[:3]} {real_code[3:]}"
                    await cdp.js(
                        "(() => {"
                        f"  document.querySelector('.code-input').value = {json.dumps(spaced)};"
                        "  document.querySelectorAll('.pair-form input')[1].value = '浏览器验收';"
                        "  document.querySelector('.pair-form').requestSubmit();"
                        "  return true;"
                        "})()"
                    )
                    await cdp.wait_for(
                        "location.hash === '#/' && document.querySelectorAll('#view .card').length > 0",
                        40,
                    )
                    check("配对成功后直接进入项目列表（不用再刷新）", True)
                    # 服务端把 cookie 标了 httponly，前端一行 JS 都不该看到它
                    check(
                        "凭证 cookie 对 JS 不可见（httponly）",
                        "ra_token" not in await cdp.js("document.cookie"),
                        await cdp.js("document.cookie"),
                    )

                    # ---- 10. 设备管理：列表 + 吊销 ----
                    # 浏览器现在是"一台配对设备"，而设备管理只有主机能做
                    # （那是"人在电脑前"的事）。换成主机凭证——桌面上的真实用法。
                    #
                    # 这一步顺带是**缓存那个坑的回归测试**：这个 URL 前面已经
                    # 打开过，文档很可能直接从缓存出（服务端连请求都收不到，
                    # Set-Cookie 不会被执行）。`adoptUrlToken()` 就是为它写的——
                    # 前端主动拿地址栏里的 token 打一次 /api/whoami 来换 cookie。
                    # 哪天有人把那段删了，这里会以一个看起来很怪的 403 挂掉。
                    await cdp.send("Page.navigate", url=f"{base}/?token={TOKEN}#/devices")
                    await cdp.wait_for("!!document.querySelector('#view .card.flat')", 30)
                    check(
                        "换主机凭证后地址栏里的 token 被抹掉了",
                        "token=" not in await cdp.js("location.href"),
                        await cdp.js("location.href"),
                    )
                    device_names = await cdp.js(
                        "[...document.querySelectorAll('#view .card.flat .name')].map(n => n.textContent)"
                    )
                    check("设备列表里有刚配对的设备", "浏览器验收" in device_names, str(device_names))

                    await cdp.js("document.querySelector('#view .card.flat button.danger').click()")
                    await cdp.wait_for(
                        "document.querySelector('#view').textContent.includes('已吊销')", 25
                    )
                    revoked_state = await cdp.js(
                        "[...document.querySelectorAll('#view .card.flat .pill')].map(p => p.textContent)"
                    )
                    check("点吊销后设备变成已吊销", any("已吊销" in str(s) for s in revoked_state),
                          str(revoked_state))
                    # 别只信 DOM：界面重画一遍也能"看起来成功"。
                    # 真吊销是**落了库**的，这个后端断言才是那句 UI 的凭据。
                    # （"吊销后旧凭证打不进来"由 verify_auth.py 用真 HTTP 验。）
                    check(
                        "吊销真的落到了库里（不只是界面重画）",
                        any(d.revoked for d in app[CTX].auth.list_devices()),
                        str([(d.name, d.revoked) for d in app[CTX].auth.list_devices()]),
                    )

                    # ---- 11. 全程没有未捕获的 JS 异常 ----
                    check(
                        "页面全程没有未捕获的 JS 异常",
                        not cdp.exceptions,
                        "；".join(cdp.exceptions[:3]),
                    )
        finally:
            if chrome is not None:
                chrome.terminate()
                try:
                    chrome.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    chrome.kill()
            shutil.rmtree(profile, ignore_errors=True)
            await relay.stop()
            await runner.cleanup()

    print("\n" + "=" * 64)
    failed = 0
    for label, passed, detail in checks:
        print(f"  {'✓' if passed else '✗'}  {label}")
        if detail:
            print(f"       {detail}")
        failed += 0 if passed else 1
    print("=" * 64)
    print(f"  {len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
