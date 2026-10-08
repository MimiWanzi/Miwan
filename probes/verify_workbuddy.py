"""WorkBuddy（CodeBuddy CLI）× 真 CLI 验证（烧额度：三四个小任务）。

零额度探针（scratch/check_workbuddy_core.py）验证的是网关这一半（假 JSONL +
假 CLI 桩）；这里验证登录后的**真 CLI** 行为，把登录前悬而未决的疑问关掉：

  A（read_only）纯文本任务——stream-json 全链、task_started 的 tools 数组、
    成本字段、会话 JSONL 真的落盘（discovery 的 read_history 能读到它）
  B（edit）写文件——tool_use/tool_result 块的真实形状（登录前只能从源码推断）、
    acceptEdits 生效（文件真的出现在磁盘上）
  C（resume_from=A）续聊——--resume 跨进程上下文保持（问 A 里埋的暗号）
  D（edit）撞权限点的命令（ping）——deny-only 不会挂死：没有审批通道的
    CLI 在无人值守时要么沙箱放行、要么静默拒绝，两种都必须走到终态
  E（full）shell 命令——--allowedTools 预批真的生效（命令零拒绝执行）。
    这节是后补的：此前 allowlist 传错形状（逗号串被当成一条字面规则），
    A/C 的暗号在 prompt 上下文里、B 靠 acceptEdits，全都绕开了 allowlist，
    "绿"掩盖了失效——E 节逼真跑一条 shell 命令，把这个盲区关掉

前置条件：CLI 已登录（凭证在 ~/.codebuddy/.credentials.json）。没登录时
result.errors 会报 Authentication required——探针会把它识别为 SKIP 提示。

额度成本：几个小任务对话，约几分钱。
"""

import asyncio
import json
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from gateway.adapters.workbuddy import discovery  # noqa: E402
from gateway.adapters.workbuddy.locate import projects_dir  # noqa: E402
from gateway.config import load_config  # noqa: E402
from gateway.server import build_app  # noqa: E402

TOKEN = "verify-workbuddy-1234567890"
HARD_PER_TASK = 300.0
TERMINAL = ("completed", "failed", "cancelled", "disconnected")

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((ok, what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}", flush=True)


def tool_output_contains(events: list[dict], needle: str) -> bool:
    """标记串是否出现在任何 tool_result 里（= 命令真的跑过，理由同 MCP 探针）。"""
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


async def drive(session, base, headers, project_id, prompt: str, autonomy: str,
                resume_from: str | None = None):
    """提交任务 → 等终态。返回 (task_id, 事件流, 终态)。"""
    body = {
        "project_id": project_id,
        "prompt": prompt,
        "agent_id": "workbuddy",
        "autonomy": autonomy,
    }
    if resume_from:
        body["resume_from"] = resume_from
    async with session.post(f"{base}/api/tasks", headers=headers, json=body) as resp:
        assert resp.status == 202, f"提交任务失败：{resp.status}"
        task = (await resp.json())["task"]
    task_id = task["id"]
    tag = f"{autonomy}" + (f" resume={resume_from[:8]}" if resume_from else "")
    print(f"  任务 {task_id} 已提交（{tag}）：{prompt[:36]}…", flush=True)

    events: list[dict] = []
    status = None
    deadline = time.monotonic() + HARD_PER_TASK
    while time.monotonic() < deadline:
        events = await dump_events(session, base, headers, task_id)
        async with session.get(f"{base}/api/tasks/{task_id}", headers=headers) as resp:
            status = (await resp.json())["task"]["status"]
        if status in TERMINAL:
            break
        await asyncio.sleep(1.0)
    return task_id, events, status


def first_payload(events: list[dict], type_: str) -> dict | None:
    for ev in events:
        if ev.get("type") == type_:
            return ev.get("payload") or {}
    return None


def payloads_of(events: list[dict], type_: str) -> list[dict]:
    return [ev.get("payload") or {} for ev in events if ev.get("type") == type_]


def failed_reason(events: list[dict]) -> str:
    p = first_payload(events, "task_failed")
    return json.dumps(p, ensure_ascii=False)[:200] if p else ""


async def _let_sessions_die() -> None:
    """等 runner 把失败任务的会话 close 完（CLI 常驻，close 走满 2s 宽限才杀）。

    _execute 一进 finally 就把自己从 _running 里 pop 掉了，所以
    runner.shutdown() 此刻等不到它——只能靠等。值取 GRACE_EXIT + 杀进程余量。
    """
    await asyncio.sleep(3.0)


async def rmtree_with_retry(path: Path, attempts: int = 10) -> None:
    """带重试的清理。

    任务终态后 runner 的 finally 还在会话 close 的 2s 宽限期内，CLI 进程
    （常驻）仍占着 proj 目录——删除撞上宽限是瞬态，不是泄漏（外部验证过
    close 后目录立即可删；shutdown 路径也会等 close 跑完）。重试盖过它即可。
    """
    import shutil
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if i == attempts - 1:
                raise
            await asyncio.sleep(1.0)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ra-verify-wb-"))
    try:
        skipped = await _run(tmp)
    finally:
        await rmtree_with_retry(tmp)
    if skipped:
        return 2
    return _finish()


def _finish() -> int:
    print("=" * 60)
    passed = sum(1 for ok, _ in results if ok)
    print(f"  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


async def _run(tmp: Path) -> bool:
    """返回 True = SKIP（环境不满足），False = 完整跑完。"""
    config = replace(load_config(), data_dir=tmp / "data", token=TOKEN)
    app = build_app(config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    headers = {"X-Token": TOKEN}
    print(f"网关起在 {base}（隔离数据目录）", flush=True)

    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        async with session.get(f"{base}/api/agents", headers=headers) as resp:
            agents = (await resp.json())["agents"]
            me = next((a for a in agents if a["id"] == "workbuddy"), None)
        if me is None or me.get("status") != "ready":
            print("SKIP：workbuddy 未就绪（{}）".format((me or {}).get("detail")))
            await _let_sessions_die()
            await runner.cleanup()
            return True
        print("workbuddy 就绪：", me.get("version") or me.get("executable"), flush=True)

        async with session.post(
            f"{base}/api/projects",
            headers=headers,
            json={"path": str(tmp / "proj"), "create": True},
        ) as resp:
            body = await resp.json()
        project_id = (body.get("project", body))["id"]

        # ---- A：read_only 纯文本 ----
        print("== A：read_only 纯文本任务 ==")
        t0 = time.monotonic()
        _, events, status = await drive(
            session, base, headers, project_id,
            "请记住暗号 WB-7391。然后只回复：好的。",
            "read_only",
        )
        started = first_payload(events, "task_started")
        check(started is not None, "task_started 到达（system/init 被归一化）")
        check(status == "completed", f"任务完成（实际 {status}，{int(time.monotonic()-t0)}s）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        if status != "completed" and "Authentication" in failed_reason(events):
            print("SKIP：CLI 未登录（.credentials.json 缺失）。先交互登录一次 codebuddy。")
            await _let_sessions_die()
            await runner.cleanup()
            return True
        texts = [p.get("text", "") for p in payloads_of(events, "assistant_text")]
        check(any("好的" in t for t in texts), f"assistant 文本到达（{texts[:1]}）")
        completed = first_payload(events, "task_completed")
        check(completed is not None and completed.get("cost_usd") is not None,
              f"成本字段到位（{completed and completed.get('cost_usd')}）")
        native_id = (started or {}).get("native_session_id")
        check(bool(native_id), f"native_session_id 落库（{native_id}）")
        if started and started.get("tools"):
            print(f"  [记录] init tools 数组：{started.get('tools')}")

        if native_id:
            history = discovery.read_history(projects_dir(), native_id)
            check(any("WB-7391" in m.get("text", "") for m in history if "text" in m),
                  "会话 JSONL 真的落盘且 read_history 读得到（持久化契约）")
            kinds = sorted({"tool" if "tool" in m else "text" for m in history})
            print(f"  [记录] JSONL 条目种类：{kinds}，共 {len(history)} 条")
        else:
            check(False, "没有 native_session_id，持久化契约无法验证")

        # ---- B：edit 写文件 ----
        print("== B：edit 写文件（tool 块真实形状） ==")
        _, events, status = await drive(
            session, base, headers, project_id,
            "在工作目录创建文件 hello.txt，内容就写 hi 两个字母。完成后只回复：写好了。",
            "edit",
        )
        check(status == "completed", f"任务完成（实际 {status}）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        tool_calls = payloads_of(events, "tool_call")
        check(bool(tool_calls),
              f"tool_use 块以 tool_call 事件到达（{[t.get('name') for t in tool_calls][:3]}）")
        check((tmp / "proj" / "hello.txt").exists(),
              "文件真的出现在磁盘上（acceptEdits 生效）")
        tool_results = payloads_of(events, "tool_result")
        check(bool(tool_results) and not any(r.get("is_error") for r in tool_results),
              "tool_result 到达且非错误")

        # ---- C：--resume 续聊 ----
        print("== C：--resume 续聊上下文 ==")
        if not native_id:
            check(False, "A 没产出 native_session_id，续聊无法验证")
        else:
            _, events, status = await drive(
                session, base, headers, project_id,
                "暗号是什么？只回复暗号本身。",
                "read_only",
                resume_from=native_id,
            )
            check(status == "completed", f"续聊任务完成（实际 {status}）")
            texts = [p.get("text", "") for p in payloads_of(events, "assistant_text")]
            check(any("WB-7391" in t for t in texts),
                  f"跨进程上下文保持：暗号答对了（{texts[:1]}）")

        # ---- D：EDIT 档撞权限点不会挂死 ----
        print("== D：deny-only 不挂死 ==")
        _, events, status = await drive(
            session, base, headers, project_id,
            "运行命令：ping -n 1 127.0.0.1，把输出原样告诉我。不要做其他事。",
            "edit",
        )
        check(status in TERMINAL and status != "disconnected",
              f"撞权限点的任务走到了终态（实际 {status}）——没有挂死")
        ttl = tool_output_contains(events, "TTL=")
        perms = payloads_of(events, "permission_request")
        if ttl:
            print("  [记录] 沙箱放行了 ping（有 TTL=），未触发拒绝路径")
        elif perms:
            print(f"  [记录] deny-only 拦截并汇报（{len(perms)} 条 permission_request，"
                  f"无 permission_id={perms[0].get('permission_id') is None}）")
        else:
            print("  [记录] 既无执行证据也无拒权汇报（模型没实际跑命令）")

        # ---- E：FULL 档 allowlist 预批真的生效（shell 命令零拒绝执行）----
        print("== E：FULL 档 allowlist 生效 ==")
        _, events, status = await drive(
            session, base, headers, project_id,
            "用 shell 工具运行命令：echo cbc-full-ok-2026，然后只回复命令的输出。不要做其他事。",
            "full",
        )
        check(status == "completed", f"任务完成（实际 {status}）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        perms = payloads_of(events, "permission_request")
        check(not perms, f"FULL 档零权限拒绝（实际 {len(perms)} 条）"
              + (f"；首条 {json.dumps(perms[0], ensure_ascii=False)[:150]}" if perms else ""))
        check(tool_output_contains(events, "cbc-full-ok-2026"),
              "shell 命令真的执行了（tool_result 含标记串）——allowlist 预批生效")

    await _let_sessions_die()
    await runner.cleanup()
    return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
