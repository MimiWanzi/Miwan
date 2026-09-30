#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 7 层的免费验证：配对、设备凭证、吊销、限速。

**不花一分钱**——全程只打 HTTP 和读库，不启动任何 Agent。

跑法：
    python probes/verify_auth.py
    python probes/verify_auth.py -v

这一层最值得防的失效模式不是"配对不成功"（那个很显眼），而是下面几种
**看起来正常**的：

  - 配对成功了，但凭证没种进 cookie → 用户每点一次都要重新配对
  - 吊销只是把设备从列表里划掉，旧凭证照样能打进来 → "手机丢了"没有真的解决
  - 配对码换到一半失败会被消耗掉 → 用户输错一次就得回电脑前重新拿码
  - 凭证明文落在库里 → 任何一次"把库拷出来看看"都等于把控制权一起拷走

所以下面每一条都对着一个具体的坏结局，而不是对着一个接口。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.api import _is_loopback  # noqa: E402
from gateway.auth import MAX_ATTEMPTS  # noqa: E402
from gateway.config import load_config  # noqa: E402
from gateway.context import CTX  # noqa: E402
from gateway.server import build_app  # noqa: E402

HOST_TOKEN = "host-token-for-tests"

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

Checks = list[tuple[str, bool, str]]


def ok(checks: Checks, label: str, passed: bool, detail: str = "") -> None:
    checks.append((label, passed, detail))


# ----------------------------------------------------------------------


class Server:
    """一个临时网关。``data_dir`` 可复用，用来测"重启后凭证还在不在"。"""

    def __init__(self, data_dir: Path, web_dir: Path | None = None) -> None:
        self.config = replace(
            load_config(),
            data_dir=data_dir,
            token=HOST_TOKEN,
            web_dir=web_dir or (ROOT / "nonexistent-web"),
        )
        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.base = ""
        self.port = 0

    async def __aenter__(self) -> "Server":
        self.app = build_app(self.config)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        self.base = f"http://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *exc) -> None:
        assert self.runner is not None
        await self.runner.cleanup()

    @property
    def auth(self):
        assert self.app is not None
        return self.app[CTX].auth

    def code(self) -> str:
        return self.auth.issue_code()


async def session() -> aiohttp.ClientSession:
    # IP 访问时 aiohttp 默认不存 cookie，这里放开——否则"配对之后不用再带凭证"
    # 这条根本测不到，而它正是这一层的全部意义
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


async def anonymous(server) -> aiohttp.ClientSession:
    """一个**没有任何凭证**的客户端。

    必须新开一个 session：``Server`` 上的中间件会给每个成功的请求种 cookie，
    所以同一个 session 走到后面手里已经有凭证了。"没凭证会怎样"这类检查
    必须用一个干净的 jar，否则测到的是"带着上一个请求留下的 cookie 会怎样"——
    而它恰好是成功的那条路径，于是一个本该 401 的场景会返回 200 或 403。
    """
    return await session()


async def call(sess, method: str, url: str, **kw):
    """发一个请求，返回 (状态码, 头, 解析后的 JSON 或 None)。"""
    async with sess.request(method, url, **kw) as resp:
        raw = await resp.text()
        headers = dict(resp.headers)
        status = resp.status
    try:
        return status, headers, json.loads(raw)
    except json.JSONDecodeError:
        return status, headers, None


# ----------------------------------------------------------------------
# 配对
# ----------------------------------------------------------------------


async def phase_pairing(checks: Checks, tmp: Path) -> str:
    """配对主流程。返回配对得到的设备凭证，给后面的阶段用。"""
    async with Server(tmp / "d1") as server:
        code = server.code()
        spaced = f"{code[:3]} {code[3:]}"
        # 留给最后的"库里到底存了什么"：这个码和下面那个凭证**都不该落盘**
        CODES["last"] = code

        async with await session() as s:
            status, _, body = await call(
                s, "POST", f"{server.base}/api/pair",
                json={"code": "000000" if code != "000000" else "111111", "name": "x"},
            )
            ok(checks, "错误配对码 → 401",
               status == 401 and body.get("error") == "pairing_rejected",
               f"HTTP {status} {body}")

            status, _, body = await call(s, "POST", f"{server.base}/api/pair", json={})
            ok(checks, "空请求体也是 401（不是 500）", status == 401, f"HTTP {status} {body}")

            # 横幅上显示的是 "123 456"，用户十有八九连着空格一起抄。
            # 服务端要是不接受这个形状，"照着屏幕上抄"这个最自然的动作会稳定失败。
            status, headers, body = await call(
                s, "POST", f"{server.base}/api/pair",
                json={"code": spaced, "name": "我的手机"},
            )
            ok(checks, "带空格的配对码 → 201", status == 201, f"HTTP {status} {body}")
            token = (body or {}).get("token") or ""

            set_cookie = headers.get("Set-Cookie", "")
            ok(checks, "配对响应直接种 cookie",
               "ra_token" in set_cookie, set_cookie or "（没有 Set-Cookie）")
            ok(checks, "cookie 是 httponly",
               "HttpOnly" in set_cookie, set_cookie)
            # secure 一开，明文 HTTP 下浏览器直接不收这个 cookie，
            # 表现是"配对成功但下一个请求 401"——排查起来要人命
            ok(checks, "cookie 没开 secure（明文 HTTP）",
               "Secure" not in set_cookie, set_cookie)
            ok(checks, "设备名被记下来了",
               (body or {}).get("device", {}).get("name") == "我的手机", str(body))

            # 上一条已经把 cookie 收进 jar 了，这一条故意不带任何凭证
            status, _, body = await call(s, "GET", f"{server.base}/api/projects")
            ok(checks, "配对之后 cookie 就能打 /api（不用再带凭证）",
               status == 200, f"HTTP {status} {body}")

            # 一次性：同一个码不能配第二台设备
            async with await session() as other:
                status, _, _ = await call(
                    other, "POST", f"{server.base}/api/pair",
                    json={"code": spaced, "name": "第二台"},
                )
            ok(checks, "配对码一次性（第二台设备用同一个码 → 401）",
               status == 401, f"HTTP {status}")

            # 配对码换完就该失效，否则"15 分钟"这个数字是假的
            ok(checks, "换完码即失效",
               server.auth.code_expired(), f"seconds_left={server.auth.code_seconds_left()}")

        return token


# ----------------------------------------------------------------------
# 设备凭证 / 吊销
# ----------------------------------------------------------------------


async def phase_devices(checks: Checks, tmp: Path, device_token: str) -> None:
    data_dir = tmp / "d1"   # 复用上一个阶段的库

    # ---- 重启：凭证必须还在。它是落库的，不是进程内存里的 ----
    async with Server(data_dir) as server:
        async with await session() as s:
            status, _, _ = await call(
                s, "GET", f"{server.base}/api/projects",
                headers={"X-Token": device_token},
            )
            ok(checks, "重启后设备凭证依然有效（凭证是落库的）",
               status == 200, f"HTTP {status}")

            status, _, body = await call(
                s, "GET", f"{server.base}/api/devices",
                headers={"X-Token": HOST_TOKEN},
            )
            devices = (body or {}).get("devices", [])
            ok(checks, "主机能看到已配对设备",
               status == 200 and len(devices) == 1, f"HTTP {status} {body}")
            ok(checks, "设备列表里没有凭证字段",
               devices and not any(k in devices[0] for k in ("token", "token_hash")),
               str(devices))

            status, _, _ = await call(
                s, "GET", f"{server.base}/api/devices",
                headers={"X-Token": device_token},
            )
            ok(checks, "普通设备管不了设备（403）", status == 403, f"HTTP {status}")

            async with await anonymous(server) as anon:
                status, _, _ = await call(anon, "GET", f"{server.base}/api/devices")
            ok(checks, "无凭证读设备列表 → 401", status == 401, f"HTTP {status}")

            status, _, body = await call(
                s, "POST", f"{server.base}/api/devices/ghost/revoke",
                headers={"X-Token": HOST_TOKEN},
            )
            ok(checks, "吊销不存在的设备 → 404", status == 404, f"HTTP {status} {body}")

            # ---- 吊销 ----
            device_id = devices[0]["id"]
            status, _, body = await call(
                s, "POST", f"{server.base}/api/devices/{device_id}/revoke",
                headers={"X-Token": HOST_TOKEN},
            )
            ok(checks, "吊销 → 200", status == 200, f"HTTP {status} {body}")

            status, _, body = await call(
                s, "GET", f"{server.base}/api/projects",
                headers={"X-Token": device_token},
            )
            ok(checks, "**吊销后旧凭证立刻失效（401 revoked）**",
               status == 401 and (body or {}).get("error") == "revoked",
               f"HTTP {status} {body}")

            # 吊销只针对那一台设备，别把主机一起误伤
            status, _, _ = await call(
                s, "GET", f"{server.base}/api/projects",
                headers={"X-Token": HOST_TOKEN},
            )
            ok(checks, "吊销不影响主机 token", status == 200, f"HTTP {status}")

            status, _, _ = await call(
                s, "POST", f"{server.base}/api/devices/{device_id}/revoke",
                headers={"X-Token": HOST_TOKEN},
            )
            ok(checks, "重复吊销 → 404（不是静默成功）", status == 404, f"HTTP {status}")


# ----------------------------------------------------------------------
# 限速——配对码只有 20 bit，限速是这个方案成立的前提，不是优化
# ----------------------------------------------------------------------


async def phase_ratelimit(checks: Checks, tmp: Path) -> None:
    async with Server(tmp / "d2") as server:
        server.code()
        async with await session() as s:
            replies = []
            for i in range(MAX_ATTEMPTS + 1):
                replies.append(await call(
                    s, "POST", f"{server.base}/api/pair",
                    json={"code": "999999", "name": f"t{i}"},
                ))

            # 前 MAX_ATTEMPTS 次是"码不对，还剩 N 次"；第 MAX_ATTEMPTS+1 次
            # 才是被限速挡下。两者是**不同**的拒绝，混为一谈的话
            # 下面两条会对着错误的那个响应做断言。
            before = replies[:MAX_ATTEMPTS]
            blocked_status, blocked_headers, blocked_body = replies[MAX_ATTEMPTS]

            details = [(r[2] or {}).get("detail", "") for r in before]
            ok(checks, f"前 {MAX_ATTEMPTS - 1} 次会告诉用户还剩几次",
               all(r[0] == 401 for r in before)
               and all("还可以试" in d for d in details[:-1]),
               str(details))
            ok(checks, f"第 {MAX_ATTEMPTS + 1} 次被挡下来",
               blocked_status == 401, f"HTTP {blocked_status}")
            ok(checks, "被挡时给出 Retry-After",
               "Retry-After" in blocked_headers, str(blocked_headers))
            ok(checks, "被挡的原因是人话（不是 'rate limited'）",
               "等" in (blocked_body or {}).get("detail", ""),
               str(blocked_body))

    # 上面那条要一个新码才测得到，所以单开一台：先错到锁死，**再拿正确的码试**。
    # 如果锁只挡错误的码，那它一点用都没有——攻击者反正只需要试到对为止。
    async with Server(tmp / "d3") as server:
        code = server.code()
        async with await session() as s:
            for i in range(MAX_ATTEMPTS):
                await call(s, "POST", f"{server.base}/api/pair",
                           json={"code": "999999", "name": f"t{i}"})
            status, _, body = await call(
                s, "POST", f"{server.base}/api/pair",
                json={"code": f"{code[:3]} {code[3:]}", "name": "正确的码"},
            )
            ok(checks, "锁住期间**正确的码也被拒**（限速不可绕过）",
               status == 401 and "还剩" not in (body or {}).get("detail", ""),
               f"HTTP {status} {body}")


# ----------------------------------------------------------------------
# 配对码只给坐在机器前的人
# ----------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, remote: str) -> None:
        self.remote = remote


async def phase_code_endpoint(checks: Checks, tmp: Path) -> None:
    async with Server(tmp / "d4") as server:
        async with await session() as s:
            status, _, body = await call(
                s, "POST", f"{server.base}/api/pairing-code",
                headers={"X-Token": HOST_TOKEN},
            )
            ok(checks, "本机换码 → 200 且能读到码",
               status == 200 and len((body or {}).get("code") or "") == 6,
               f"HTTP {status} {body}")

            async with await anonymous(server) as anon:
                status, _, _ = await call(anon, "POST", f"{server.base}/api/pairing-code")
            ok(checks, "无凭证换码 → 401", status == 401, f"HTTP {status}")

    # 来源判定本身。远程调用**必须**拿不到码：那是"码只显示在
    # 攻击者看不到的那块屏幕上"这条原则的唯一执行点。
    ok(checks, "127.0.0.1 算本机", _is_loopback(_FakeRequest("127.0.0.1")))
    ok(checks, "::1 算本机", _is_loopback(_FakeRequest("::1")))
    ok(checks, "局域网地址不算本机", not _is_loopback(_FakeRequest("192.168.1.50")))
    ok(checks, "None（拿不到来源）不算本机", not _is_loopback(_FakeRequest(None)))


# ----------------------------------------------------------------------
# 库里到底存了什么
# ----------------------------------------------------------------------


async def phase_storage(checks: Checks, tmp: Path) -> None:
    """凭证明文绝不能落盘。

    这个库会被打开来看（手机上要审计谁干了什么）。原文一旦躺在里面，
    任何一次"把库拷出来看看"都等于把凭证一起拷走了。

    扫描时要把 ``-wal`` 也读上：库跑在 WAL 模式下，刚写的东西
    可能一个字都还没进主库文件。
    """
    data_dir = tmp / "d1"
    token = TOKENS.get("device", "")
    code = CODES.get("last", "")

    blob = b""
    for name in ("gateway.db", "gateway.db-wal"):
        path = data_dir / name
        if path.exists():
            blob += path.read_bytes()

    ok(checks, "库文件读得到", len(blob) > 0, f"{len(blob)} 字节")
    ok(checks, "**设备凭证明文不在库里**",
       token and token.encode() not in blob,
       "凭证原文出现在了数据库文件里")
    ok(checks, "配对码不落库",
       code and code.encode() not in blob,
       "配对码出现在了数据库文件里")
    ok(checks, "存的是凭证的 sha256",
       hashlib.sha256(token.encode()).hexdigest().encode() in blob,
       "库里没有找到凭证的哈希")


# ----------------------------------------------------------------------

TOKENS: dict[str, str] = {}
CODES: dict[str, str] = {}


async def main() -> int:
    checks: Checks = []

    with tempfile.TemporaryDirectory(prefix="ra-auth-") as raw:
        tmp = Path(raw)
        TOKENS["device"] = await phase_pairing(checks, tmp)
        await phase_devices(checks, tmp, TOKENS["device"])
        await phase_ratelimit(checks, tmp)
        await phase_code_endpoint(checks, tmp)
        await phase_storage(checks, tmp)

    failed = 0
    for label, passed, detail in checks:
        if not passed:
            print(f"  ✗  {label}\n       → {detail}")
            failed += 1
        elif VERBOSE:
            print(f"  ✓  {label}")
    print(f"{len(checks) - failed}/{len(checks)} 项通过"
          + ("" if not failed else "  ← 有失败"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
