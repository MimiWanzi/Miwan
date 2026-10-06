"""权限审批通道：把 CLI 的权限提示变成手机上的一个「允许 / 拒绝」。

工作方式：任务以 EDIT / FULL 档启动时，runner 会给 CLI 传
``--permission-prompt-tool mcp__perm__request_permission`` 和一份指向本模块的
MCP 配置（见 ``session._write_mcp_config``）。CLI 撞到权限点时调用这个工具，
HTTP 请求走到这里，转手交给 :meth:`TaskRunner.request_permission` ——
它落库、把任务标成 WAITING_APPROVAL、发事件，然后**阻塞等待**手机上的决策。

这个模块只做协议翻译（JSON-RPC ↔ runner 调用），不认识 SQLite 也不发事件——
它是 HTTP 层的一部分，和 :mod:`gateway.api` 同级。

认证：URL 上必须带 runner 发的每任务密钥（``?key=``）。鉴权中间件对
``/mcp/`` 不要求设备凭证——CLI 手里没有设备凭证，密钥就是它的身份。
密钥能做的只有"为它自己的任务发起审批"，真正的批准/拒绝（``/api/permissions``
的决策接口）永远要设备凭证。
"""

from __future__ import annotations

import json

from aiohttp import web

from .context import get_ctx

routes = web.RouteTableDef()

#: CLI 端等一次工具应答没有我们的超时概念，真正的时限由
#: :attr:`TaskRunner.PROMPT_TIMEOUT` 掌握（到点自动拒绝）。
_PROMPT_TIMEOUT: float | None = None

TOOLS = [
    {
        "name": "request_permission",
        "description": (
            "Ask the human operator to approve or deny a tool call. "
            "Returns allow/deny with an optional reason."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "input": {"type": "object"},
            },
            "required": ["tool_name"],
        },
    }
]


def _rpc_result(request_id, result) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(request_id, code: int, message: str) -> web.Response:
    return web.json_response(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


@routes.post("/mcp/{task_id}")
async def mcp_endpoint(request: web.Request) -> web.Response:
    """MCP streamable-HTTP 端点（POST）。

    只实现这个工具服务用得到的子集：initialize 握手、tools/list、tools/call。
    通知（无 id 的请求）一律 202——协议上服务器不该回应通知。
    """
    ctx = get_ctx(request)
    task_id = request.match_info["task_id"]
    if not ctx.runner.validate_prompt_key(task_id, request.query.get("key") or ""):
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 —— 坏请求只配得到一个简短的四百
        return _rpc_error(None, -32700, "解析错误：请求体不是合法 JSON")
    if isinstance(body, list):
        return _rpc_error(None, -32600, "不支持批量请求")
    if not isinstance(body, dict):
        return _rpc_error(None, -32600, "无效请求")

    method = body.get("method")
    request_id = body.get("id")

    if method == "initialize":
        # protocolVersion 原样回给客户端：版本协商由客户端决定接受与否，
        # 服务端硬编码一个版本反而会在 CLI 升级时把自己锁死。
        return _rpc_result(
            request_id,
            {
                "protocolVersion": body.get("params", {}).get("protocolVersion")
                or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "remote-agent-permissions", "version": "0.1.0"},
            },
        )

    if method is None or request_id is None:
        # 通知：initialized / cancelled 等都不需要回应内容
        return web.Response(status=202)

    if method == "tools/list":
        return _rpc_result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        params = body.get("params") or {}
        if params.get("name") != "request_permission":
            return _rpc_error(request_id, -32602, f"未知工具：{params.get('name')}")
        args = params.get("arguments") or {}
        decision = await ctx.runner.request_permission(
            task_id,
            str(args.get("tool_name") or "?"),
            args.get("input") or {},
            timeout=_PROMPT_TIMEOUT,
        )
        return _rpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(decision)}],
                "isError": False,
            },
        )

    return _rpc_error(request_id, -32601, f"未知方法：{method}")


@routes.get("/mcp/{task_id}")
async def mcp_stream(request: web.Request) -> web.Response:
    """客户端可能按 streamable-HTTP 规范先 GET 开一条 SSE 流等服务器推送。

    我们从不主动推送（所有应答都随 POST 返回），405 让客户端走纯 POST 模式。
    """
    return web.Response(status=405)


@routes.delete("/mcp/{task_id}")
async def mcp_close(request: web.Request) -> web.Response:
    """会话拆除。无状态实现，直接应答成功。"""
    return web.Response(status=200)
