"""MCP 权限审批通道 × 真 CLI 验证（烧额度：两个真实 Claude Code 任务）。

零额度探针（scratch/check_mcp_approval_http.py）验证的是网关这一半；
这里验证另一半、也是唯一剩下的未验证依赖：**真 claude 撞到权限点时，
会不会真的调用 ``--permission-prompt-tool`` 指向的 MCP 工具，并服从应答**。

权限触发命令的讲究（scratch/check_perm_trigger.py 实测）：
  - 裸 echo 会被沙箱自动放行，根本撞不到权限点；
  - 写 cwd 之外是硬安全墙（blocked for security），不走审批；
  - 网络访问（ping）才是标准的 "requires approval" 权限点。
所以命令用 ping 127.0.0.1。执行证据用输出里的 ``TTL=``：tool_result
内容由 CLI 生成、模型伪造不了，被拒时命令没跑自然没有 TTL——
不另外 echo 标记，因为模型被拒后可能单独跑 echo，而 echo 会被沙箱
自动放行，把 deny 组的断言污染掉。

  任务 1（allow）：EDIT 档下 ping——CLI 必须走审批通道。探针等
    permission_request 事件出现（等不到 = CLI 不认这个参数，这正是
    要抓的），自动批准，验证 ping 真的执行了（tool_result 有 TTL=）。
  任务 2（deny）：同样撞审批点，自动拒绝——验证 ping 没有被执行，
    且任务仍正常走到终态（拒绝不是崩溃）。
  应答是循环式的：模型被拒后可能换姿势重试、再次撞点，所以探针
    应答**所有** permission_request，而不是只应答第一条。

额度成本：两次小任务对话，约几分钱。
"""

import asyncio
import json
import socket
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
from gateway.server import build_app  # noqa: E402

TOKEN = "verify-mcp-approval-1234567890"
HARD_PER_TASK = 300.0
TERMINAL = ("completed", "failed", "cancelled", "disconnected")

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((ok, what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}", flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def tool_output_contains(events: list[dict], needle: str) -> bool:
    """标记字符串是否出现在任何 tool_result 里（= 命令真的跑过）。

    只看 tool_result：assistant 文本可能复述或脑补命令输出，那不算执行过。
    """
    for ev in events:
        if ev.get("type") == "tool_result":
            if needle in json.dumps(ev.get("payload", {}), ensure_ascii=False):
                return True
    return False


async def dump_events(session, base, headers, task_id) -> list[dict]:
    async with session.get(
        f"{base}/api/tasks/{task_id}/events?since=0", headers=headers
    ) as resp:
        return (await resp.json()).get("events", [])


async def drive(session, base, headers, project_id, decision: str):
    """提交任务 → 循环应答所有审批请求 → 等到终态。

    返回 (task_id, 事件流, 终态, 第一条 permission_request 或 None)。
    """
    prompt = (
        "请运行命令：ping -n 1 127.0.0.1，"
        "然后把命令的输出原样告诉我。不要做任何其他事情。"
    )
    async with session.post(
        f"{base}/api/tasks",
        headers=headers,
        json={
            "project_id": project_id,
            "prompt": prompt,
            "agent_id": "claude_code",
            "autonomy": "edit",
        },
    ) as resp:
        assert resp.status == 202, f"提交任务失败：{resp.status}"
        task = (await resp.json())["task"]
    task_id = task["id"]
    print(f"  任务 {task_id} 已提交（期望决策：{decision}）", flush=True)

    answered: set[str] = set()
    first_perm = None
    events: list[dict] = []
    status = None
    deadline = time.monotonic() + HARD_PER_TASK

    while time.monotonic() < deadline:
        events = await dump_events(session, base, headers, task_id)
        async with session.get(f"{base}/api/tasks/{task_id}", headers=headers) as resp:
            status = (await resp.json())["task"]["status"]

        for ev in events:
            if ev.get("type") != "permission_request":
                continue
            # mapper 会把 result.permission_denials 也 emit 成 permission_request
            # （任务收尾时的拒绝记录，没有 permission_id，不可也不需应答）
            pid = (ev.get("payload") or {}).get("permission_id")
            if not pid or pid in answered:
                continue
            if first_perm is None:
                first_perm = ev
            answered.add(pid)
            print(f"  → 审批请求 perm={pid} tool={ev['payload'].get('tool_name')}"
                  f" input={json.dumps(ev['payload'].get('tool_input', {}), ensure_ascii=False)[:120]}"
                  f" → {decision}", flush=True)
            async with session.post(
                f"{base}/api/permissions/{pid}/decision",
                headers=headers, json={"decision": decision},
            ) as resp:
                assert resp.status == 200, f"决策失败：{resp.status}"

        if status in TERMINAL:
            break
        await asyncio.sleep(1.0)

    return task_id, events, status, first_perm


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ra-verify-mcp-") as tmp:
        # 端口必须先定死：审批回调 URL 在 build_app 里用 config.port 拼出来，
        # 真 CLI 拿到的就是这个 URL，TCPSite 必须真的绑在同一个端口上
        config = replace(
            load_config(), data_dir=Path(tmp) / "data", token=TOKEN, port=free_port()
        )

        app = build_app(config)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", config.port)
        await site.start()
        base = f"http://127.0.0.1:{config.port}"
        headers = {"X-Token": TOKEN}

        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True)
        ) as session:
            async with session.get(f"{base}/api/agents", headers=headers) as resp:
                agents = (await resp.json())["agents"]
                me = next((a for a in agents if a["id"] == "claude_code"), None)
            if me is None or me.get("status") != "ready":
                print("SKIP：claude_code 未就绪（{}），真 CLI 验证无法进行".format(
                    (me or {}).get("detail")))
                await runner.cleanup()
                return 2
            print("claude_code 就绪：", me.get("version") or me.get("executable"), flush=True)

            async with session.post(
                f"{base}/api/projects",
                headers=headers,
                json={"path": str(Path(tmp) / "proj"), "create": True},
            ) as resp:
                body = await resp.json()
            project_id = (body.get("project", body))["id"]

            print("== 任务 1：撞审批点 → 批准 ==")
            t0 = time.monotonic()
            task_id, events, status, ev = await drive(
                session, base, headers, project_id, "allow"
            )
            check(ev is not None,
                  "CLI 真的调用了审批通道（permission_request 到达）——未验证依赖确认")
            if ev is not None:
                check(status == "completed", f"批准后任务完成（实际 {status}，{int(time.monotonic()-t0)}s）")
                check(tool_output_contains(events or [], "TTL="),
                      "命令真的执行了：tool_result 里有 ping 的 TTL= 输出")
                requests = [e for e in events if e["type"] == "permission_request"]
                decided = [e for e in events if e["type"] == "permission_decided"]
                check(len(requests) == len(decided) > 0
                      and all(d["payload"]["decision"] == "allow" for d in decided),
                      f"permission_request/decided 成对且全为 allow"
                      f"（{len(requests)} 对）")
            else:
                check(False, "任务 1 没有等到审批请求")
                print("  [诊断] 任务事件流：", flush=True)
                for e in events or []:
                    print(f"    {e.get('type')}: "
                          f"{json.dumps(e.get('payload', {}), ensure_ascii=False)[:220]}",
                          flush=True)
                async with session.get(
                    f"{base}/api/tasks/{task_id}", headers=headers
                ) as resp:
                    print("  [诊断] 任务状态：",
                          (await resp.json())["task"]["status"], flush=True)
                await session.post(f"{base}/api/tasks/{task_id}/cancel", headers=headers)

            print("== 任务 2：撞审批点 → 拒绝 ==")
            t0 = time.monotonic()
            task_id, events, status, ev = await drive(
                session, base, headers, project_id, "deny"
            )
            if ev is not None:
                check(status == "completed",
                      f"拒绝后任务仍正常走到终态，不是失败（实际 {status}，{int(time.monotonic()-t0)}s）")
                check(not tool_output_contains(events or [], "TTL="),
                      "被拒绝的命令真的没有执行（tool_result 里没有 TTL= 输出）")
                decided = [e for e in events if e["type"] == "permission_decided"]
                check(bool(decided) and all(d["payload"]["decision"] == "deny" for d in decided),
                      f"所有决策都是 deny（{len(decided)} 条）")
            else:
                check(False, "任务 2 没有等到审批请求")
                print("  [诊断] 任务事件流：", flush=True)
                for e in events or []:
                    print(f"    {e.get('type')}: "
                          f"{json.dumps(e.get('payload', {}), ensure_ascii=False)[:220]}",
                          flush=True)
                await session.post(f"{base}/api/tasks/{task_id}/cancel", headers=headers)

        await runner.cleanup()

    print("=" * 60)
    passed = sum(1 for ok, _ in results if ok)
    print(f"  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
