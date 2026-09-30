"""REST 路由。

三条约定，加字段时不会破客户端：

  1. 响应永远是 JSON **对象**，不是裸数组。以后想加 ``total`` / ``has_more``
     之类的旁路信息时不必改客户端的解析方式
  2. 错误统一 ``{"error": "..."}``，配恰当的 HTTP 状态码
  3. 这里**只做 HTTP 的事**——取参数、定状态码、序列化。
     业务判断（项目在不在、Agent 认不认识）都交给 ``store`` / ``runner``，
     它们抛出的异常在这里翻译成状态码。写在 HTTP 层的话 CLI 就绕过了这些规则。

分层的另一面：**这个模块认 aiohttp，runner / store / adapters 一个都不认**。
把局域网换成隧道时改动只在这里和 ``ws``。
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from .adapters import DEFAULT_AUTONOMY, AutonomyPolicy, detect_all
from .auth import PairingRejected, set_token_cookie
from .context import get_ctx, get_device
from .models import TERMINAL_STATUSES
from .runner import RunnerError, UnknownProject
from .sync import refresh

routes = web.RouteTableDef()

DEFAULT_TASK_LIMIT = 50
MAX_TASK_LIMIT = 500
DEFAULT_EVENT_LIMIT = 1000
MAX_EVENT_LIMIT = 2000


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def dumps_json(value: object, *args: object, **kwargs: object) -> str:
    """不转义非 ASCII：手机上抓包或直接看接口时，中文是可读的。

    aiohttp 自己默认用 ``json.dumps``，也就是 ``ensure_ascii=True``。
    落在这一条上的地方比看上去多：REST 响应、鉴权失败、**以及 WebSocket 的每一帧**。
    所以它是公开的——``ws`` 也要用同一个，分头定义迟早会有一处漏掉，
    而那种不一致只有肉眼抓包才看得出来。
    """
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(value, *args, **kwargs)  # type: ignore[arg-type]


def json_response(data: dict, *, status: int = 200) -> web.Response:
    """统一的 JSON 响应。

    ``server`` 的错误中间件和鉴权也用它——同一个约定只该有一处定义，
    否则 401 会转义中文而 404 不会。
    """
    return web.json_response(data, status=status, dumps=dumps_json)


def json_error(message: str, status: int) -> web.Response:
    return json_response({"error": message}, status=status)


def _dump(model) -> dict:
    """pydantic 模型 → 可直接 json 序列化的 dict（枚举转字符串、时间戳保持数字）。"""
    return model.model_dump(mode="json")


def _int_param(
    request: web.Request, name: str, default: int, low: int, high: int
) -> int:
    """取整型查询参数。**非法值退回默认而不是报错**——

    页码类的参数写错时，给一个合理的结果比给一个 400 更有用；
    真正需要严格校验的是会改状态的输入（见 create_task）。
    """
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(low, min(high, value))


async def _body(request: web.Request) -> dict | None:
    """读 JSON 请求体。不是对象就返回 None（由调用方决定怎么报错）。"""
    if not request.can_read_body:
        return None
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ----------------------------------------------------------------------
# 配对 / 设备（第 7 层）
# ----------------------------------------------------------------------


@routes.post("/api/pair")
async def pair(request: web.Request) -> web.Response:
    """用配对码换一份设备凭证。

    **这是唯一一条不需要凭证就能打的接口**，因为它的作用正是发放凭证；
    要求先有凭证就成了死循环。它靠配对码自证：短时、一次性、有尝试次数上限。

    凭证在响应里**只出现这一次**——库里存的是哈希，丢了只能重新配对。
    """
    ctx = get_ctx(request)
    body = await _body(request)
    if body is None:
        return json_error("请求体必须是 JSON 对象", 400)

    code = str(body.get("code") or "")
    name = str(body.get("name") or "")
    # 按来源限速。局域网里 peername 足够用，不用理会 X-Forwarded-For——
    # 那是给反向代理用的，而这里没有代理，信它反而给了伪造来源的办法。
    source = request.remote or "?"

    try:
        paired = ctx.auth.pair(code, name, source=source)
    except PairingRejected as exc:
        headers = {}
        if exc.retry_after:
            headers["Retry-After"] = str(int(exc.retry_after))
        return web.json_response(
            {"error": "pairing_rejected", "detail": exc.reason},
            status=401,
            headers=headers,
            dumps=dumps_json,
        )

    response = json_response(
        {
            "device": paired.device.as_dict(),
            "token": paired.token,
            "detail": "凭证只显示这一次，已经存进这个浏览器了。",
        },
        status=201,
    )
    # **cookie 由这次响应直接种下**，前端一行 JS 都不碰那个 token。
    # 让前端自己存的话有两条路，两条都更差：写 document.cookie 拿不到
    # httponly；用 ?token= 再跳一次会把长期凭证放进地址栏和历史记录——
    # 那正是第 7 层要消灭的东西。
    #
    # 响应体里同样带着 token（curl / 将来的原生客户端要它），
    # 但浏览器那条路用不上，前端刻意不读。
    set_token_cookie(response, paired.token)
    return response


@routes.get("/api/devices")
async def list_devices(request: web.Request) -> web.Response:
    """列出配对过的设备。

    只允许主机 token 调用：设备管理是"人在电脑前"做的事。放开给普通设备的话，
    一台被偷的手机就能把用户其它设备全踢下线。
    """
    denied = _host_only(request)
    if denied is not None:
        return denied
    ctx = get_ctx(request)
    return json_response(
        {
            "devices": [d.as_dict() for d in ctx.auth.list_devices()],
            "pairing": {
                "active": not ctx.auth.code_expired(),
                "seconds_left": round(ctx.auth.code_seconds_left()),
            },
        }
    )


@routes.post("/api/devices/{device_id}/revoke")
async def revoke_device(request: web.Request) -> web.Response:
    """吊销一台设备。同样只允许主机 token。"""
    denied = _host_only(request)
    if denied is not None:
        return denied
    ctx = get_ctx(request)
    device_id = request.match_info["device_id"]
    if not ctx.auth.revoke(device_id):
        return json_error("没有这台设备，或者它已经被吊销了", 404)
    return json_response({"ok": True, "id": device_id})


def _is_loopback(request: web.Request) -> bool:
    """请求是不是从本机发起的。

    这是"能不能把配对码读回去"的分界线。``request.remote`` 是 TCP 层的对端地址
    ——它没法伪造（要伪造得先真的建立一条从那个地址来的连接）。所以这里不看
    ``X-Forwarded-For``：局域网里没有反向代理，信那个头只会给出一条伪造来源的路。

    主机 token 确实是更强的凭证，但持有它的人未必坐在机器前——用户完全可能
    把 ``data/token.txt`` 拷到别处去用。而配对码的原则是"只显示在攻击者看不到的
    那块屏幕上"，所以那个位置必须由**网络位置**来证明，不能由凭证来证明。
    """
    return request.remote in ("127.0.0.1", "::1", "localhost")


@routes.post("/api/pairing-code")
async def new_pairing_code(request: web.Request) -> web.Response:
    """换一个新的配对码。

    默认**不返回码本身**：码要显示在网关那台机器的屏幕上，那是唯一一个攻击者
    看不到的地方。走 HTTP 返回的话，配对码就和一个不带锁的凭证一样了。

    只有从本机发起的请求例外——那条路是 ``python -m gateway code``，
    用来救"终端关了/码过期了"。它不算破例：能连上 127.0.0.1 的人已经在
    这台机器上了，屏幕对他不是秘密。
    """
    denied = _host_only(request)
    if denied is not None:
        return denied

    ctx = get_ctx(request)
    code = ctx.auth.issue_code(force=True)

    if not _is_loopback(request):
        return json_response(
            {
                "ok": True,
                "detail": "新配对码已打到网关那台机器的屏幕上（远程调用看不到它）",
            }
        )
    return json_response(
        {
            "ok": True,
            "code": code,
            "seconds_left": round(ctx.auth.code_seconds_left()),
            "detail": "配对码只显示这一次，关掉就再要一个。",
        }
    )


def _host_only(request: web.Request) -> web.Response | None:
    """设备管理只让主机 token 进来。

    判断的是**凭证本身是不是主机 token**，不是"有没有认证"——
    认证过的普通设备也要挡住。
    """
    device = get_device(request)
    if device is None or device.id != "host":
        return json_error("只有主机 token 能管理设备（在网关那台机器上操作）", 403)
    return None


# ----------------------------------------------------------------------
# 健康 / Agent
# ----------------------------------------------------------------------


@routes.get("/api/whoami")
async def whoami(request: web.Request) -> web.Response:
    """我是谁——当前这份凭证对应哪台设备、是不是主机。

    这条路由的存在理由是**别把凭证挂在文档响应上**。

    ``?token=`` 只能靠"页面响应"来种 cookie，而页面响应恰好是浏览器最爱缓存的
    东西。用 CDP 实测过：同一个 ``/?token=…`` 第二次打开时 Chrome 直接从缓存取
    文档，``transferSize`` 是 0、服务端访问日志里连这条请求都没有，
    **于是 Set-Cookie 根本没被重新应用**。给响应加上 ``Cache-Control: no-store``
    也没有改变结果（观察到的事实如此，成因未深究）。后果很具体：cookie 过期后
    点书签，URL 明明带着有效凭证，人却被弹回配对页。

    所以前端改成主动来这条路由换：XHR 响应没有可缓存的验证器，中文档那份
    只当兜底。顺带它也是 UI 判断"要不要显示设备管理入口"的正经依据——
    比去试探 ``/api/devices`` 是不是 403 干净得多。
    """
    device = get_device(request)
    return json_response(
        {
            "device": device.as_dict() if device is not None else None,
            "host": bool(device is not None and device.id == "host"),
        }
    )


@routes.get("/api/health")
async def health(request: web.Request) -> web.Response:
    """给客户端（和运维）一个廉价的存活探针。

    刻意**不查库**：这个接口会被手机定期敲，任何一次磁盘卡顿都不该让它变慢。
    """
    ctx = get_ctx(request)
    return json_response(
        {
            "ok": True,
            "running_tasks": ctx.runner.running_count,
            "subscribers": ctx.bus.subscriber_count,
        }
    )


@routes.get("/api/agents")
async def list_agents(request: web.Request) -> web.Response:
    """本机装了哪些 Agent、各自可用不可用、具备什么能力。

    ``detect_all()`` 会起子进程（``claude --version``），所以必须下线程池——
    这个接口被手机打开首页时就会调，卡住事件循环的代价是整个网关一起卡。
    """
    infos = await asyncio.to_thread(detect_all)
    return json_response({"agents": [_dump(info) for info in infos]})


# ----------------------------------------------------------------------
# 项目 / 会话
# ----------------------------------------------------------------------


@routes.get("/api/projects")
async def list_projects(request: web.Request) -> web.Response:
    """读**库里**的项目，不触发扫描。

    扫描要好几秒（每个 Agent 都要起进程），首页加载等不起。
    要最新的就显式 POST /api/projects/refresh。
    """
    ctx = get_ctx(request)
    return json_response({"projects": [_dump(p) for p in ctx.store.list_projects()]})


@routes.post("/api/projects/refresh")
async def refresh_projects(request: web.Request) -> web.Response:
    """重新扫描所有 Agent 的项目存储，落库，返回刷新后的列表。

    没有可用的接入时**不报 503**：库里可能有上次扫到的项目，UI 照样该显示。
    把失败原因放在 ``detail`` 里让它提示，比让整个列表变空有用。
    """
    ctx = get_ctx(request)
    result = await refresh(ctx.store, ctx.config.blocked_paths)
    payload: dict = {
        "projects": [_dump(p) for p in ctx.store.list_projects()],
        "agents": [a.agent_id for a in result.adapters],
        "skipped_blocked": result.scan.skipped_blocked,
        "removed": result.removed,
    }
    if not result.adapters:
        payload["detail"] = "没有可用的 Agent 接入，本次只读了库里的旧数据"
    return json_response(payload)


@routes.get("/api/projects/{project_id}")
async def project_detail(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    project_id = request.match_info["project_id"]
    project = ctx.store.get_project(project_id)
    if project is None:
        return json_error(f"没有这个项目：{project_id}", 404)

    sessions = ctx.store.list_sessions(project_id)
    return json_response(
        {
            "project": _dump(project),
            "sessions": [_dump(s) for s in sessions],
        }
    )


# ----------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------


@routes.get("/api/tasks")
async def list_tasks(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    project_id = request.query.get("project") or None
    limit = _int_param(request, "limit", DEFAULT_TASK_LIMIT, 1, MAX_TASK_LIMIT)

    tasks = ctx.store.list_tasks(project_id)
    return json_response(
        {
            "tasks": [_dump(t) for t in tasks[:limit]],
            # total 是裁剪前的数量：前端要显示"共 N 条"时不必再发一次请求
            "total": len(tasks),
        }
    )


@routes.post("/api/tasks")
async def create_task(request: web.Request) -> web.Response:
    """提交任务。**立即返回 202，不等它跑完。**

    这是"手机下完命令马上能走"的前提：如果这个接口阻塞到任务结束，
    客户端一断线就什么都没了，而且一个跑 10 分钟的任务会占住一条 HTTP 连接。
    事件从 ``/ws`` 或 ``/api/tasks/{id}/events`` 拿。
    """
    ctx = get_ctx(request)
    body = await _body(request)
    if body is None:
        return json_error("请求体必须是 JSON 对象", 400)

    project_id = str(body.get("project_id") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    if not project_id:
        return json_error("缺少 project_id", 400)
    if not prompt:
        return json_error("prompt 不能为空", 400)

    try:
        # 不传 autonomy 就是"照常工作"（用户定的策略：默认放行 + 手机只收通知）。
        # 用 DEFAULT_AUTONOMY 而不是写死 FULL —— 默认值在四处出现（这里、runner、
        # CLI、前端下拉框），散着写迟早有一处忘了改，而那种错没有报错、没有日志。
        autonomy = AutonomyPolicy(body.get("autonomy") or DEFAULT_AUTONOMY.value)
    except ValueError:
        options = "、".join(p.value for p in AutonomyPolicy)
        return json_error(f"autonomy 只能是 {options}", 400)

    try:
        # 刻意**不在这里检查 Agent 是否就绪**：那要起一次 claude --version，
        # 每个任务多花一秒左右，而提交接口是要立刻返回的。
        # 不可用时 _execute 会很快失败，并作为事件出现在事件流里——
        # 前端该用 /api/agents 提前把不可用的接入置灰，而不是让这里来兜底。
        task = ctx.runner.submit(
            project_id=project_id,
            prompt=prompt,
            agent_id=body.get("agent_id") or None,
            autonomy=autonomy,
            resume_from=body.get("resume_from") or None,
            session_id=body.get("session_id") or None,
        )
    except UnknownProject as exc:
        return json_error(str(exc), 404)
    except RunnerError as exc:
        return json_error(str(exc), 400)

    return json_response({"task": _dump(task)}, status=202)


@routes.get("/api/tasks/{task_id}")
async def task_detail(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    task_id = request.match_info["task_id"]
    task = ctx.store.get_task(task_id)
    if task is None:
        return json_error(f"没有这个任务：{task_id}", 404)

    return json_response(
        {
            "task": _dump(task),
            "event_count": ctx.store.event_count(task_id),
        }
    )


@routes.get("/api/tasks/{task_id}/events")
async def task_events(request: web.Request) -> web.Response:
    """按 seq 取事件。**这就是断线补发本身**，WebSocket 只是它的推送版本。

    单独存在有两个用处：一是手机网络很差时可以降级成轮询；
    二是第 8 层验证重连时，可以拿它和 WebSocket 补发的序列逐条比对。
    """
    ctx = get_ctx(request)
    task_id = request.match_info["task_id"]
    if ctx.store.get_task(task_id) is None:
        return json_error(f"没有这个任务：{task_id}", 404)

    since = _int_param(request, "since", 0, 0, 2**62)
    limit = _int_param(request, "limit", DEFAULT_EVENT_LIMIT, 1, MAX_EVENT_LIMIT)
    events = ctx.store.events_since(task_id, since, limit=limit)

    return json_response(
        {
            "events": [_dump(e) for e in events],
            # 一条都没有时要回 since 而不是 0，否则客户端会把游标倒回去重收一遍
            "last_seq": events[-1].seq if events else since,
            "has_more": len(events) == limit,
        }
    )


@routes.post("/api/tasks/{task_id}/cancel")
async def cancel_task(request: web.Request) -> web.Response:
    """中断正在跑的任务。

    注意它**只发出中断请求就返回**，任务真正进入 cancelled 要等一会儿——
    客户端应该盯事件流里的终态，而不是把这里的 200 当作已经结束了。
    """
    ctx = get_ctx(request)
    task_id = request.match_info["task_id"]
    task = ctx.store.get_task(task_id)
    if task is None:
        return json_error(f"没有这个任务：{task_id}", 404)
    if task.status in TERMINAL_STATUSES:
        return json_error(f"任务已经结束了（{task.status.value}），无法中断", 409)

    if not await ctx.runner.cancel(task_id):
        # 状态是 running 但进程里没这个会话：只可能来自上次启动，
        # 那种任务已经在 recover() 里标成 disconnected 了，这里再拦一道是为了
        # 万一漏了也不给用户一个"取消成功了"的假象。
        return json_error("该任务不在本进程里运行（来自上次启动），无法中断", 409)

    return json_response({"cancelled": True, "task_id": task_id})
