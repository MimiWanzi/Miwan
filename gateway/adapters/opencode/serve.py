"""OpenCode serve 进程的共享管理。

**一个网关只有一个 serve 进程**，所有 OpenCode 任务共用：它按请求头/查询参数
切目录，天然支持多项目并存。起一个进程的成本不小（bun 冷启动数秒），
按任务起会让每个任务的头部都多出这段时间。

生命周期：第一次 ``acquire()`` 时惰性启动；网关关停时经
:meth:`ServeManager.shutdown` 显式杀掉——Windows 上子进程不会随父进程退出，
不杀就是孤儿进程，持续占着端口和内存。
"""

from __future__ import annotations

import asyncio
import socket
from collections import deque

import aiohttp

from . import discovery


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServeManager:
    #: bun 冷启动 + serve 就绪实测 ~8s（首次还会拉模型目录），留足余量；
    #: 起不来比等得久更糟——报错里要带上 stderr 尾巴，否则无从排查。
    START_TIMEOUT = 90.0

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._http: aiohttp.ClientSession | None = None
        self._base: str | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._lock = asyncio.Lock()

    async def acquire(self) -> tuple[aiohttp.ClientSession, str]:
        """返回 ``(HTTP 会话, serve 的 base_url)``；服务没起就起一个。

        并发任务同时进入时会串行地拿到**同一个**服务——锁保证不重复起进程。
        """
        async with self._lock:
            if (
                self._http is not None
                and self._proc is not None
                and self._proc.returncode is None
                and self._base is not None
            ):
                return self._http, self._base
            await self._start()
            return self._http, self._base  # type: ignore[return-value]  # _start 成功后必非 None

    async def _start(self) -> None:
        await self._stop()

        command = discovery.find_command()
        if command is None:
            raise RuntimeError(
                "找不到 OpenCode 启动命令：PATH 上没有 opencode，"
                "config.toml 也没配 [opencode] command"
            )
        port = _free_port()
        self._proc = await asyncio.create_subprocess_exec(
            *command,
            "serve",
            "--port",
            str(port),
            "--hostname",
            "127.0.0.1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15)
        )
        self._base = f"http://127.0.0.1:{port}"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.START_TIMEOUT
        while loop.time() < deadline:
            if self._proc.returncode is not None:
                tail = "；".join(self._stderr_tail) or "（stderr 无输出）"
                raise RuntimeError(f"OpenCode serve 启动即退出（码 {self._proc.returncode}）：{tail}")
            try:
                async with self._http.get(f"{self._base}/project") as resp:
                    if resp.status == 200:
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.3)
        raise RuntimeError(f"OpenCode serve 在 {self.START_TIMEOUT:g}s 内没有就绪")

    async def _drain_stderr(self) -> None:
        """必须持续排空 stderr，否则缓冲区写满会把子进程卡死（同 Claude 的教训）。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._stderr_tail.append(line)

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None
        self._base = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                await proc.wait()
            except asyncio.CancelledError:
                pass


#: 全网关共享的实例。Adapter 与它建出的所有会话都从这里拿服务。
manager = ServeManager()
