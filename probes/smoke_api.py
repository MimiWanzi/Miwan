#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 4 层的免费冒烟测试：只打不花钱的接口，不跑真实任务。

`verify_api.py` 会真实调用一次 Claude（消耗额度）。这个脚本是它的前置关卡——
参数校验、状态码、静态资源这些低级错误不该用真金白银去发现。

跑法：
    python probes/smoke_api.py          全部断言，退出码 0/1
    python probes/smoke_api.py -v       连每一条的响应体一起打出来

它**带断言**，不是只打印。只打印的检查一旦没人盯着看就等于没有，
而这里每一行的期望值都是确定的，没有"要看情况"的余地。

其中「静态资源会不会抢走 /api」这一项：第 4 层跑起来时 `web/` 还不存在，
那条分支根本没被执行过，要到第 5 层把前端文件放进去才第一次生效。
那种"上一层埋、下一层炸"的问题最难查，所以这里**手造一个 web/ 把它逼出来**。
"""

from __future__ import annotations

import asyncio
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

TOKEN = "smoke-token"

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

Checks = list[tuple[str, bool, str]]


async def expect(
    checks: Checks,
    label: str,
    call,
    status: int,
    contains: str | None = None,
) -> str:
    """打一个请求，断言状态码（和可选的响应片段）。返回响应体。"""
    resp = await call()
    body = await resp.text()
    ok = resp.status == status
    if ok and contains is not None:
        ok = contains in body
    detail = f"HTTP {resp.status}（期望 {status}）"
    if ok and contains is not None and contains not in body:
        detail += f"；响应里没有 {contains!r}"
    checks.append((label, ok, detail))
    if VERBOSE:
        print(f"    {label:26} {resp.status}  {body[:110]}")
    return body


async def run_app(config, body) -> None:
    """起一个临时服务，跑 body(checks, session, base, port)，然后收干净。"""
    app = build_app(config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    try:
        # 用 IP 访问时 aiohttp 默认不收 cookie，这里显式放开——否则
        # "种了 cookie 就不必再带 token"这条根本测不到
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True)
        ) as session:
            await body(session, base, port)
    finally:
        await runner.cleanup()


# ----------------------------------------------------------------------
# 没有前端文件时的兜底（web/ 不存在）
# ----------------------------------------------------------------------


async def check_no_frontend(checks: Checks, session, base: str, port: int) -> None:
    await expect(checks, "GET / 退回接口清单", lambda: session.get(f"{base}/"), 200, "Gateway")

    await expect(checks, "health 免 token", lambda: session.get(f"{base}/api/health"), 200, '"ok"')
    await expect(checks, "无 token → 401", lambda: session.get(f"{base}/api/projects"), 401, "unauthorized")
    await expect(
        checks, "?token= → 200", lambda: session.get(f"{base}/api/projects?token={TOKEN}"), 200, "projects"
    )
    # 上一条已经种了 cookie，这一条故意不带 token
    await expect(checks, "cookie 生效（不用再带 token）", lambda: session.get(f"{base}/api/projects"), 200)
    await expect(checks, "X-Token 请求头", lambda: session.get(f"{base}/api/tasks", headers={"X-Token": TOKEN}), 200)
    await expect(
        checks,
        "Bearer 请求头",
        lambda: session.get(f"{base}/api/tasks", headers={"Authorization": f"Bearer {TOKEN}"}),
        200,
    )
    await expect(checks, "错误 token → 401", lambda: session.get(f"{base}/api/projects?token=nope"), 401)
    await expect(checks, "refresh", lambda: session.post(f"{base}/api/projects/refresh"), 200, "projects")
    await expect(checks, "agents", lambda: session.get(f"{base}/api/agents"), 200, "agents")


async def check_errors(checks: Checks, session, base: str) -> None:
    await expect(checks, "项目 404", lambda: session.get(f"{base}/api/projects/nope"), 404)
    await expect(checks, "任务 404", lambda: session.get(f"{base}/api/tasks/nope"), 404)
    await expect(checks, "取消 404", lambda: session.post(f"{base}/api/tasks/nope/cancel"), 404)
    await expect(checks, "事件 404", lambda: session.get(f"{base}/api/tasks/nope/events"), 404)
    await expect(
        checks,
        "请求体不是 JSON → 400",
        lambda: session.post(
            f"{base}/api/tasks", data=b"nope", headers={"Content-Type": "application/json"}
        ),
        400,
    )
    await expect(
        checks,
        "prompt 为空 → 400",
        lambda: session.post(f"{base}/api/tasks", json={"project_id": "x", "prompt": "  "}),
        400,
        "prompt",
    )
    await expect(
        checks,
        "未知项目 → 404",
        lambda: session.post(f"{base}/api/tasks", json={"project_id": "nope", "prompt": "hi"}),
        404,
    )
    await expect(
        checks,
        "未知 Agent → 400",
        lambda: session.post(
            f"{base}/api/tasks", json={"project_id": "nope", "prompt": "hi", "agent_id": "x"}
        ),
        400,
        "Agent",
    )
    await expect(
        checks,
        "autonomy 非法 → 400",
        lambda: session.post(
            f"{base}/api/tasks", json={"project_id": "nope", "prompt": "hi", "autonomy": "yolo"}
        ),
        400,
        "autonomy",
    )


async def check_cookie_bootstrap(checks: Checks, base: str) -> None:
    """用 ?token= 打开页面之后，页面自己发出的请求不该再要 token。

    这是**第 5 层才会暴露**的一条：第 4 层的所有检查都带着 token 请求，
    所以"打开 / 的时候 cookie 到底种没种下"根本没被测到。
    前端一上来就是 `fetch('/api/projects')`，不种 cookie 就是 401，
    而页面本身是好的——看起来像前端写错了。

    独立开一条 session：主 session 到这一步已经有 cookie 了，测不出东西。
    """
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as fresh:
        await expect(checks, "全新会话：先打开 /", lambda: fresh.get(f"{base}/?token={TOKEN}"), 200)
        await expect(
            checks, "紧跟着的 /api 请求（不再带 token）", lambda: fresh.get(f"{base}/api/projects"), 200
        )


async def check_ws(checks: Checks, session, base: str, port: int) -> None:
    async with session.ws_connect(f"ws://127.0.0.1:{port}/ws?task_id=nope") as ws:
        msg = await ws.receive(timeout=10)
    text = str(msg.data)
    checks.append(("WS 未知任务 → error 帧", '"type": "error"' in text, text[:140]))
    # 中文不能被转义成 \uXXXX：手机上看事件流就是为了看这些字
    checks.append(("WS 帧里的中文可读", "没有这个任务" in text, text[:140]))
    if VERBOSE:
        print(f"    {'WS 未知任务':26} {text[:110]}")


# ----------------------------------------------------------------------
# 有前端文件：第 5 层的场景
# ----------------------------------------------------------------------


async def check_real_frontend(checks: Checks, session, base: str) -> None:
    """挂上仓库里**真正的前端**之后，静态资源不能把 /api 抢走。

    第 4 层跑起来的时候 web/ 还不存在，那条分支根本没被执行过。
    这里用真文件而不是手造的假文件：手造的假的能验证挂载顺序，
    验证不了"app.js 真的在、真的能取到"——而后者才是第 5 层会踩的坑。
    """
    await expect(checks, "GET / 给 index.html", lambda: session.get(f"{base}/"), 200, "Keeper")
    await expect(checks, "app.js", lambda: session.get(f"{base}/app.js"), 200, "openStream")
    await expect(checks, "style.css", lambda: session.get(f"{base}/style.css"), 200, "--bg")
    await expect(checks, "/api/health 不被抢走", lambda: session.get(f"{base}/api/health"), 200, '"ok"')
    # 带 token：不然先是 401，测到的就不是"404 仍是 JSON"这件事
    await expect(
        checks,
        "/api 的 404 仍是 JSON",
        lambda: session.get(f"{base}/api/tasks/nope?token={TOKEN}"),
        404,
        "error",
    )


# ----------------------------------------------------------------------


async def main() -> int:
    checks: Checks = []

    with tempfile.TemporaryDirectory(prefix="ra-smoke-") as tmp:
        base_config = replace(load_config(), data_dir=Path(tmp) / "data", token=TOKEN)

        # 起两次。第一次把 web/ 指到一个不存在的目录，专门验"还没有前端"时的兜底；
        # 第二次用仓库里真实的 web/，验前端文件本身和静态挂载不会抢走 /api。
        # 分开是必须的：前端一旦存在，兜底那条路径就再也走不到了。
        no_web = replace(base_config, web_dir=Path(tmp) / "no-such-web")

        async def body_no_frontend(session, base, port):
            await check_no_frontend(checks, session, base, port)
            await check_errors(checks, session, base)
            await check_cookie_bootstrap(checks, base)
            await check_ws(checks, session, base, port)

        await run_app(no_web, body_no_frontend)

        real_web = replace(base_config, data_dir=Path(tmp) / "data2")
        await run_app(real_web, lambda s, base, port: check_real_frontend(checks, s, base))

    failed = 0
    for label, passed, detail in checks:
        if not passed:
            print(f"  ✗  {label}\n       → {detail}")
        failed += 0 if passed else 1
    print(f"{len(checks) - failed}/{len(checks)} 项通过" + ("" if not failed else "  ← 有失败"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
