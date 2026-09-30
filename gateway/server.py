"""HTTP / WebSocket 服务的装配与生命周期。

"网关是一个常驻服务"这件事，带来了三样在 CLI 里根本不存在的东西：

  1. **启动时要 recover()**。上次进程留下的 running 任务不会自己消失，
     不处理的话手机上永远看到"运行中"。第 3 层已经写好了 ``runner.recover()``，
     这里只负责把它接到启动点上。
  2. **关停时要收干净**。Ctrl-C 走 ``runner.shutdown()`` → 各会话 ``close()``，
     保证不留下仍在跑、仍在烧额度的 claude 子进程。
  3. **事件循环不能被阻塞**。``detect()`` 要起子进程（最长 20s）。在 CLI 里
     无所谓，在服务器里它会让所有连接一起停摆。凡是要调它的地方都下线程池
     （``/api/agents`` 和 ``sync.discover``）。

分层依然是干净的：这个模块认识 aiohttp，而 ``runner`` / ``store`` / ``adapters``
**一个都不认识**。把局域网直连换成云隧道时，改动只落在 server / api / ws。

认证说明：第 7 层之前这里是**最小可用的 token 校验**（一个 token 走天下）。
第 7 层把凭证拆成了两套——手机会用配对码换一份**自己的**设备凭证，
而主机 token 退居"人在电脑前"的根凭证。真正的逻辑在 ``auth.py``，
这里只做三件事：把请求上的凭证认出来、认出被吊销的设备、以及顺手种 cookie。

仍然**没做**的是 HTTPS：局域网内这些凭证是可以被嗅探的。这是用户 2026-09-30
明确决定推迟的，记在 ``auth.py`` 的已知限制里，不是漏掉。
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import sys
from pathlib import Path

from aiohttp import web

from . import api, ws
from .api import json_response
from .auth import TOKEN_COOKIE, AuthService, set_token_cookie
from .config import Config
from .context import CTX, DEVICE_KEY, GatewayContext, get_ctx
from .events import EventBus
from .permissions import load_policy
from .runner import TaskRunner
from .store import Store

log = logging.getLogger("gateway.server")

#: 不需要 token 的路径。
#:
#: ``/api/health``：要在"客户端还没拿到 token"时也能问。
#: ``/api/pair``：**配对本身就是拿到凭证的过程**，要求先有凭证是个死循环。
#: 它靠配对码自证——一个短时、一次性、有尝试次数上限的码。
PUBLIC_PATHS = frozenset({"/api/health", "/api/pair"})


# ----------------------------------------------------------------------
# 装配
# ----------------------------------------------------------------------


def build_app(config: Config) -> web.Application:
    """建 aiohttp 应用。**不启动它**——测试里要能先接线再 listen。"""
    store = Store(config.db_path)
    bus = EventBus()
    runner = TaskRunner(store, bus, policy=load_policy(config.data_dir))
    auth = AuthService(store, host_token=config.token)
    ctx = GatewayContext(
        config=config, store=store, bus=bus, runner=runner, auth=auth
    )

    # 顺序即嵌套顺序：error 在外层，才能把 auth 里抛出的意外也变成 JSON
    app = web.Application(middlewares=[_error_middleware, _auth_middleware])
    app[CTX] = ctx

    app.router.add_get("/", _index)
    app.add_routes(api.routes)
    app.add_routes(ws.routes)
    _mount_static(app, config.web_dir)

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    app.on_cleanup.append(_on_cleanup)
    return app


def _mount_static(app: web.Application, web_dir: Path) -> None:
    """挂前端静态文件。

    第 5 层之前 ``web/`` 根本不存在，所以这里必须容得下"没有前端"这件事——
    否则第 4 层就没办法独立启动了，而它本来是可以单独验收的。

    注册在最后：aiohttp 按注册顺序匹配，先命中的是上面那些精确路由，
    静态前缀只在其余路径上兜底。
    """
    if not web_dir.is_dir():
        return
    app.router.add_static("/", web_dir, name="web", show_index=False, append_version=False)


# ----------------------------------------------------------------------
# 中间件
# ----------------------------------------------------------------------


@web.middleware
async def _error_middleware(request: web.Request, handler):
    """把未捕获的异常变成 JSON。

    默认的 500 是一整页 HTML traceback：手机端解析不了，也没法显示。
    真正的栈照旧打进服务端日志——那里才是看栈的地方。
    """
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 —— 中间件的职责就是兜住一切
        log.exception("处理 %s %s 时出错", request.method, request.path)
        return json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    ctx = get_ctx(request)
    presented = _presented_token(request)
    device = ctx.auth.authenticate(presented)

    # 被吊销的设备拿着真凭证，但**不再被接受**。
    # 这一步必须在 handler 之前，而且要和"凭证不认识"分开报——
    # 用户看到"手机丢了，我已经吊销了"和"这个 token 不对"要做的事完全不同。
    if device is not None and device.revoked:
        return json_response(
            {
                "error": "revoked",
                "detail": f"这台设备（{device.name}）已被吊销，请重新配对",
            },
            status=401,
        )

    authorized = device is not None
    if _needs_token(request.path) and not authorized:
        return json_response(
            {"error": "unauthorized", "detail": "缺少或不正确的 token"},
            status=401,
        )

    if authorized:
        ctx.auth.touch(device)
        # 挂到 request 上，handler 要用它判断"你是谁"（尤其是设备管理那几条
        # 路由只让主机 token 进来）。不用 request.app 传：那是全局的，
        # 一个请求的身份存进去就会污染另一个请求。
        request[DEVICE_KEY] = device

    response = await handler(request)

    # 只要这个请求证明过凭证是对的，就顺手种个 cookie，
    # **包括那些本身不需要 token 的路径**。这一条是有讲究的：
    # 用户打开页面走的正是 / ——它不需要 token（前端外壳不是秘密，
    # 而且第一次带 ?token= 时连页面本身都还没拿到，不能要求 token）。
    # 如果只在受保护的路径上种 cookie，就会变成"页面能打开，但它接下来
    # 发的第一个 fetch 是 401"——问题出在服务端，看起来却像前端写错了。
    # 不开 secure：局域网是明文 HTTP，开了浏览器直接不收。
    #
    # 种的是**出示的那个凭证**，不是主机 token：设备拿自己的凭证来换，
    # cookie 里就该是它自己的，否则吊销这台设备时它的 cookie 依然有效。
    #
    # ``/api/pair`` 不经过这里（它没有可出示的凭证），它自己种——
    # 见 ``api.pair``。两条路都要种，所以 cookie 的属性只定义在 ``auth`` 一处。
    if (
        authorized
        and presented
        and not request.path.startswith("/ws")
        and request.cookies.get(TOKEN_COOKIE) != presented
    ):
        set_token_cookie(response, presented)
        # **种了凭证的响应不能被缓存。**
        #
        # 浏览器命中 HTTP 缓存时**不会重新应用 Set-Cookie**，而入口文档
        # （``/``）是带 Last-Modified / ETag 的静态文件，会被缓存。后果很具体：
        # 用户收藏了 ``/?token=…``，30 天后 cookie 过期，再点书签——
        # 文档从缓存里出来了，凭证没种下，人直接被弹回配对页。
        # URL 明明是对的，界面却在要配对码，这种误导很难查。
        #
        # 放在这里而不是 ``_index`` 里：种 cookie 的路径不止一条（任何带
        # ``?token=`` 的请求都会），约束跟着动作走，才不会漏掉新加的那条。
        response.headers["Cache-Control"] = "no-store"
    return response


def _needs_token(path: str) -> bool:
    """``/api`` 与 ``/ws`` 要 token，静态资源不要。

    前端外壳（html/js/css）不是秘密，而且它**必须**能在没有 token 时加载——
    否则第一次带 ``?token=`` 打开页面，反而连页面本身都拿不到。
    """
    if path in PUBLIC_PATHS:
        return False
    return path == "/ws" or path.startswith("/api/")


def _presented_token(request: web.Request) -> str | None:
    """依次找 token：查询串 → 请求头 → cookie。

    查询串排第一，是为了"从聊天软件里点开一个链接"这个场景：
    手机浏览器没法方便地设请求头，而 cookie 第一次还没有。
    """
    query = request.query.get("token")
    if query:
        return query
    header = request.headers.get("X-Token")
    if header:
        return header
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.cookies.get(TOKEN_COOKIE)


# ----------------------------------------------------------------------
# 首页
# ----------------------------------------------------------------------

_STUB_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Remote Agent Workspace</title>
<style>
 body{font:15px/1.7 system-ui,sans-serif;margin:0;padding:24px;max-width:720px;
      background:#14161a;color:#e6e8eb}
 h1{font-size:19px;margin:0 0 4px} p{color:#9aa3ad;margin:0 0 20px}
 code{background:#22262d;padding:2px 6px;border-radius:4px;font-size:13px}
 table{border-collapse:collapse;width:100%} td{padding:6px 0;vertical-align:top}
 td:first-child{width:11em;white-space:nowrap}
 .m{color:#6ee7a8;font-weight:600}
</style></head><body>
<h1>Remote Agent Workspace · Gateway</h1>
<p>接口已经在跑。没找到 <code>web/index.html</code>，先给一份清单，
好让接口能被手工点一遍。</p>
<table>
 <tr><td><code>GET</code> <code>/api/health</code></td><td class="m">免 token</td></tr>
 <tr><td><code>POST</code> <code>/api/pair</code></td><td class="m">免 token — 用配对码换设备凭证</td></tr>
 <tr><td><code>GET</code> <code>/api/devices</code></td><td>已配对设备（仅主机 token）</td></tr>
 <tr><td><code>POST</code> <code>/api/devices/{id}/revoke</code></td><td>吊销一台设备（仅主机 token）</td></tr>
 <tr><td><code>POST</code> <code>/api/pairing-code</code></td><td>换新配对码（仅主机 token）</td></tr>
 <tr><td><code>GET</code> <code>/api/whoami</code></td><td>这份凭证是谁（用来种 cookie）</td></tr>
 <tr><td><code>GET</code> <code>/api/agents</code></td><td>本机 Agent 与能力</td></tr>
 <tr><td><code>GET</code> <code>/api/projects</code></td><td>库里的项目</td></tr>
 <tr><td><code>POST</code> <code>/api/projects/refresh</code></td><td>重新扫描落库</td></tr>
 <tr><td><code>GET</code> <code>/api/projects/{id}</code></td><td>项目 + 会话</td></tr>
 <tr><td><code>GET</code> <code>/api/tasks</code></td><td><code>?project=</code> <code>&amp;limit=</code></td></tr>
 <tr><td><code>POST</code> <code>/api/tasks</code></td><td>提交任务，立即返回 202</td></tr>
 <tr><td><code>GET</code> <code>/api/tasks/{id}</code></td><td>任务详情</td></tr>
 <tr><td><code>GET</code> <code>/api/tasks/{id}/events</code></td><td>按 seq 取事件（补发）</td></tr>
 <tr><td><code>POST</code> <code>/api/tasks/{id}/cancel</code></td><td>中断</td></tr>
 <tr><td><code>WS</code> <code>/ws?task_id=&amp;since=</code></td><td>实时推送</td></tr>
</table>
</body></html>
"""


async def _index(request: web.Request) -> web.Response:
    index = get_ctx(request).config.web_dir / "index.html"
    if index.is_file():
        return web.FileResponse(index)
    return web.Response(text=_STUB_PAGE, content_type="text/html", charset="utf-8")


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------


async def _on_startup(app: web.Application) -> None:
    ctx = app[CTX]
    orphans = ctx.runner.recover()
    if orphans:
        log.warning(
            "上次启动遗留了 %d 个未结束的任务，已标记为 disconnected（不会自动重跑）：%s",
            len(orphans),
            "、".join(t.id for t in orphans),
        )


async def _on_shutdown(app: web.Application) -> None:
    # 在套接字关掉之前先收任务：否则那些 claude 子进程会变成孤儿，
    # 继续跑、继续烧额度，而手机上看不到任何东西
    await app[CTX].runner.shutdown()


async def _on_cleanup(app: web.Application) -> None:
    app[CTX].store.close()


# ----------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------


def run_server(config: Config) -> int:
    """阻塞式启动，Ctrl-C 退出。返回进程退出码。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    app = build_app(config)
    # 横幅要配对码，而配对码在 AuthService 里——它是在 build_app 内部建的。
    # 从 app 上取回来而不是在外面再建一个 AuthService：两个实例各有一份
    # 配对码和一份失败计数，横幅上显示的那个码永远也配不上对。
    #
    # **先绑端口，后打印。** 顺序反过来是个真踩过的坑：``issue_code()`` 是
    # **签发**一个码，横幅一打印，端口被占时用户就先看到一条完完整整的说明
    # 加一个格式正确的码，绑定失败反而出现在它后面。拿着那个码去手机上输，
    # 得到的是"配对码不对，还可以试 4 次"——一个"端口被占"的问题就此伪装成
    # 了"你自己抄错了"。（``cmd_code`` 的注释里写着要避免的正是这个。）
    sock = _bind(config.host, config.port)
    if sock is None:
        return 1

    _print_banner(config, app[CTX])
    web.run_app(
        app,
        sock=sock,  # 端口已经绑好了，交给 aiohttp 去 accept
        print=None,  # 关掉 aiohttp 自己的横幅，用上面那个信息更全的
        access_log_format='%a "%r" %s %b %Tf',
    )
    return 0


def _bind(host: str, port: int) -> socket.socket | None:
    """把端口占下来。占不到就说明白为什么，返回 ``None``。

    ``SO_REUSEADDR`` 只在 POSIX 上设，和 asyncio 自己的默认一致。Windows 上
    设了它不是"允许重用"，而是**允许别的进程抢同一个端口**——绑定会假装成功，
    然后两个程序抢同一批连接，行为变得无法解释。这里要的恰恰是抢不到就报错。
    """
    try:
        family, socktype, proto, _, addr = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )[0]
    except socket.gaierror as exc:
        print(f"监听地址看不懂：{host}（{exc}）", file=sys.stderr)
        return None

    sock = socket.socket(family, socktype, proto)
    try:
        if os.name == "posix" and sys.platform != "cygwin":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(addr)
    except OSError as exc:
        sock.close()
        where = f"{host}:{port}"
        if exc.errno == errno.EADDRINUSE:
            print(f"起不来：{where} 已经被别的程序占着了。\n", file=sys.stderr)
            print("换一个端口：", file=sys.stderr)
            print(f"    python -m gateway serve --port {port + 1}\n", file=sys.stderr)
            print("想知道是谁占着（Windows）：", file=sys.stderr)
            print(f"    netstat -ano | findstr :{port}", file=sys.stderr)
        elif exc.errno == errno.EACCES:
            print(f"起不来：{where} 不允许绑定。1024 以下的端口要管理员权限，"
                  f"换一个高位的：python -m gateway serve --port 8765", file=sys.stderr)
        else:
            print(f"起不来：{where} 绑不上——{exc}", file=sys.stderr)
        return None
    return sock


def _lan_ip() -> str | None:
    """猜一个手机能访问到的本机地址。

    用 UDP connect 到公网地址：它**不发包**，只是让内核选出出口网卡，
    所以断网环境也照样能用。拿不到就返回 None——纯离线也是正常情况。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _print_banner(config: Config, ctx: GatewayContext) -> None:
    """启动横幅。**这里显示的配对码就是这一层存在的理由。**

    以前横幅打的是一条 ``http://192.168.x.x:8765/?token=…``。那条链接的问题
    不是 token 不够长，而是它注定会被粘进聊天软件——一旦发出去，这台机器的
    完整控制权就躺在别人的服务器上了。所以横幅改成打一个 6 位配对码：
    它短到可以手打，短命到 15 分钟作废，而且**只能换一台设备**。

    地址栏里的 ``?token=`` 仍然能用（主机 token 依然有效，命令行玩家不该被
    挡在门外），但横幅不再替你生成那条链接——默认路径必须是安全的那条。
    """
    port = config.port
    code = ctx.auth.issue_code()
    lines = [
        "",
        "Remote Agent Workspace · Gateway",
        "─" * 64,
        f"  本机    http://127.0.0.1:{port}/",
    ]
    lan = _lan_ip()
    if lan:
        lines.append(f"  局域网  http://{lan}:{port}/")

    web_note = "" if (config.web_dir / "index.html").is_file() else "（没找到 web/index.html，退回接口清单）"
    lines += [
        "",
        "  手机上打开上面的地址，输入这个配对码：",
        "",
        f"          {code[:3]} {code[3:]}          ← 15 分钟内有效，只能配一台设备",
        "",
        f"  静态页面  {config.web_dir} {web_note}".rstrip(),
        f"  数据库    {config.db_path}",
        f"  主机凭证  {config.token_file}（换码：python -m gateway code）",
        "",
        "  配对码不见了就打 `python -m gateway code` 重新要一个。",
        "  Ctrl-C 停止。",
        "─" * 64,
        "",
    ]
    # flush=True：stdout 不是终端时（`serve > log.txt`、被别的进程拉起来）
    # Python 会改成块缓冲，横幅要等缓冲区满才出现——而它里面是**配对码**，
    # 恰恰是启动后立刻就要看到的东西。
    print("\n".join(lines), flush=True)
