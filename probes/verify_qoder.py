"""Qoder（@qodercn-ai/qoderclicn）× 真 CLI 验证（烧 credits：六个小任务）。

零额度探针（scratch/check_qoder_core.py）验证的是网关这一半（假 stream-json
+ 假 CLI 桩）；这里验证登录后的**真 CLI** 行为，把考古实测（T1–T14，见项目
记忆）没有覆盖到的「真网关链路」关掉：

  A（read_only）纯文本——真 stream-json 全链、init 的 tools 数组、credits 口径
    （cost_usd 恒 None，credits 才是真实消耗）
  B（edit）写文件——工作区内写自动放行（accept_edits），文件真的出现在磁盘
  C（resume_from=A）续聊——``-r`` 跨进程云端续接。Qoder 会话正文不落本地
    （history_discovery=False），续接全靠这条，必须真验证
  D（full）工作区内 shell 命令——零权限事件 + 标记串真的出现在 tool_result。
    WorkBuddy 的假绿教训：暗号必须逼真跑在工具输出里，不能只靠上下文回答
  E（full）越界写主目录——权限门真链路：PERMISSION_REQUEST 到达 →
    /api/permissions 批准 → 文件真写到主目录。permission_gate=True 的依据
  F（full）中断——POST /cancel → 控制协议 interrupt → cancelled

前置条件：npm i -g @qodercn-ai/qoderclicn，且 qodercn login 已完成
（CLI 与桌面端凭证不互通）。没登录时任务会报 Not logged in，探针识别为 SKIP。

额度成本：六个小任务，约 2–3 credits。
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

TOKEN = "verify-qoder-1234567890"
HARD_PER_TASK = 300.0
TERMINAL = ("completed", "failed", "cancelled", "disconnected")

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((ok, what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}", flush=True)


def tool_output_contains(events: list[dict], needle: str) -> bool:
    """标记串是否出现在任何 tool_result 里（= 命令真的跑过）。"""
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
                resume_from: str | None = None, decision: str | None = None):
    """提交任务 → 等终态。``decision`` 给定时循环应答审批请求（E 节用）。

    返回 (task_id, 事件流, 终态, 第一条带 permission_id 的审批请求或 None)。
    """
    body = {
        "project_id": project_id,
        "prompt": prompt,
        "agent_id": "qoder",
        "autonomy": autonomy,
    }
    if resume_from:
        body["resume_from"] = resume_from
    async with session.post(f"{base}/api/tasks", headers=headers, json=body) as resp:
        assert resp.status == 202, f"提交任务失败：{resp.status}"
        task = (await resp.json())["task"]
    task_id = task["id"]
    tag = autonomy + (f" resume={resume_from[:8]}" if resume_from else "")
    print(f"  任务 {task_id} 已提交（{tag}）：{prompt[:36]}…", flush=True)

    answered: set[str] = set()
    first_perm = None
    events: list[dict] = []
    status = None
    deadline = time.monotonic() + HARD_PER_TASK
    while time.monotonic() < deadline:
        events = await dump_events(session, base, headers, task_id)
        async with session.get(f"{base}/api/tasks/{task_id}", headers=headers) as resp:
            status = (await resp.json())["task"]["status"]

        if decision is not None:
            for ev in events:
                if ev.get("type") != "permission_request":
                    continue
                # 任务收尾时的拒绝记录没有 permission_id，不可也不需应答
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
    """等 runner 把任务会话 close 完（常驻 CLI 进程 close 走满宽限期才杀）。"""
    await asyncio.sleep(3.0)


async def rmtree_with_retry(path: Path, attempts: int = 10) -> None:
    import shutil
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if i == attempts - 1:
                raise
            await asyncio.sleep(1.0)


def _finish() -> int:
    print("=" * 60)
    passed = sum(1 for ok, _ in results if ok)
    print(f"  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ra-verify-qoder-"))
    try:
        skipped = await _run(tmp)
    finally:
        await rmtree_with_retry(tmp)
    if skipped:
        return 2
    return _finish()


async def _run(tmp: Path) -> bool:
    # 端口必须先选定再进 config：prompt_base_url（MCP 审批回调）按
    # config.port 拼出来，隔离实例绑临时端口的话门就指向不存在的地址
    # （E 节会因此整段失败——第一次跑就栽在这里）。
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    config = replace(load_config(), data_dir=tmp / "data", token=TOKEN, port=port)
    app = build_app(config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"X-Token": TOKEN}
    print(f"网关起在 {base}（隔离数据目录）", flush=True)

    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        async with session.get(f"{base}/api/agents", headers=headers) as resp:
            agents = (await resp.json())["agents"]
            me = next((a for a in agents if a["id"] == "qoder"), None)
        if me is None or me.get("status") != "ready":
            print("SKIP：qoder 未就绪（{}）".format((me or {}).get("detail")))
            await _let_sessions_die()
            await runner.cleanup()
            return True
        print("qoder 就绪：", me.get("version") or me.get("executable"), flush=True)

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
        _, events, status, _ = await drive(
            session, base, headers, project_id,
            "请记住暗号 QD-5182。然后只回复：好的。",
            "read_only",
        )
        started = first_payload(events, "task_started")
        check(started is not None, "task_started 到达（system/init 被归一化）")
        check(status == "completed", f"任务完成（实际 {status}，{int(time.monotonic()-t0)}s）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        if status != "completed" and "Not logged in" in failed_reason(events):
            print("SKIP：CLI 未登录。先跑一次 qodercn login（与桌面端不互通）。")
            await _let_sessions_die()
            await runner.cleanup()
            return True
        texts = [p.get("text", "") for p in payloads_of(events, "assistant_text")]
        check(any("好的" in t for t in texts), f"assistant 文本到达（{texts[:1]}）")
        completed = first_payload(events, "task_completed")
        check(completed is not None and completed.get("cost_usd") is None,
              f"cost_usd 如实 None（credits 口径）")
        check(completed is not None and completed.get("credits") is not None
              and completed["credits"] > 0,
              f"credits 到位（{completed and completed.get('credits')}）")
        native_id = (started or {}).get("native_session_id")
        check(bool(native_id), f"native_session_id 落库（{native_id}）")
        if started and started.get("tools"):
            print(f"  [记录] init tools 数组（{len(started['tools'])} 个）："
                  f"{started.get('tools')}")

        # ---- B：edit 写文件 ----
        print("== B：edit 工作区内写 ==")
        _, events, status, _ = await drive(
            session, base, headers, project_id,
            "在工作目录创建文件 qoder-b.txt，内容只写 QB-7774。然后只回复：完成。",
            "edit",
        )
        check(status == "completed", f"任务完成（实际 {status}）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        target = tmp / "proj" / "qoder-b.txt"
        check(target.is_file() and "QB-7774" in target.read_text(encoding="utf-8"),
              "文件真的写进工作区（accept_edits 生效）")
        perms = payloads_of(events, "permission_request")
        print(f"  [记录] 权限事件 {len(perms)} 条"
              + (f"：{[p.get('source') for p in perms]}" if perms else "（工作区内写没走门）"))

        # ---- C：resume 续聊（云端会话存储）----
        print("== C：resume_from=A 跨进程续接 ==")
        _, events, status, _ = await drive(
            session, base, headers, project_id,
            "我让你记住的暗号是什么？只回答暗号本身。",
            "read_only", resume_from=native_id,
        )
        check(status == "completed", f"任务完成（实际 {status}）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        texts = [p.get("text", "") for p in payloads_of(events, "assistant_text")]
        check(any("QD-5182" in t for t in texts),
              f"云端续接拿到 A 轮上下文（{texts[:1]}）")

        # ---- D：full 工作区内命令（假绿教训：逼真跑）----
        print("== D：full 工作区内 shell 命令 ==")
        _, events, status, _ = await drive(
            session, base, headers, project_id,
            "用 Bash 工具运行命令：echo qd-full-ok-2026，然后只回复命令的输出。"
            "不要做其他事。",
            "full",
        )
        check(status == "completed", f"任务完成（实际 {status}）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        perms = payloads_of(events, "permission_request")
        check(not perms, f"工作区内命令零权限事件（实际 {len(perms)} 条）")
        check(tool_output_contains(events, "qd-full-ok-2026"),
              "命令真的执行了（tool_result 含标记串）")

        # ---- E：越界写主目录（权限门真链路）----
        print("== E：full 越界写 → 审批门 → 批准 ==")
        home_file = Path.home() / "qoder-e2e-home.txt"
        home_file.unlink(missing_ok=True)
        _, events, status, first_perm = await drive(
            session, base, headers, project_id,
            "用 Bash 工具运行命令：echo QD-HOME-9183 > ~/qoder-e2e-home.txt，"
            "然后告诉我命令是否成功。不要做其他事情。",
            "full", decision="allow",
        )
        check(first_perm is not None,
              "越界写触发审批请求（PERMISSION_REQUEST 带 permission_id）")
        if first_perm is not None:
            p = first_perm.get("payload") or {}
            print(f"  [记录] 请求来源 {p.get('source')}，工具 {p.get('tool_name')}，"
                  f"input={json.dumps(p.get('tool_input', {}), ensure_ascii=False)[:160]}")
        decided = payloads_of(events, "permission_decided")
        check(any(d.get("decision") == "allow" for d in decided),
              f"批准落到 PERMISSION_DECIDED（{[d.get('decision') for d in decided]}）")
        check(status == "completed", f"任务完成（实际 {status}）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        check(home_file.is_file() and "QD-HOME-9183" in home_file.read_text(encoding="utf-8"),
              "批准后文件真的写到主目录")
        home_file.unlink(missing_ok=True)

        # ---- F：中断 ----
        print("== F：full 长任务中断 ==")
        body = {
            "project_id": project_id,
            "prompt": "用 Bash 工具运行命令：ping -n 15 127.0.0.1，运行完告诉我结果。",
            "agent_id": "qoder",
            "autonomy": "full",
        }
        async with session.post(f"{base}/api/tasks", headers=headers, json=body) as resp:
            assert resp.status == 202, f"提交任务失败：{resp.status}"
            task_id = (await resp.json())["task"]["id"]
        print(f"  任务 {task_id} 已提交（等待 Bash 真跑起来）", flush=True)
        cancelled_ok = False
        deadline = time.monotonic() + HARD_PER_TASK
        sent_cancel = False
        while time.monotonic() < deadline:
            events = await dump_events(session, base, headers, task_id)
            async with session.get(f"{base}/api/tasks/{task_id}", headers=headers) as resp:
                status = (await resp.json())["task"]["status"]
            if not sent_cancel and payloads_of(events, "tool_call"):
                sent_cancel = True
                async with session.post(
                    f"{base}/api/tasks/{task_id}/cancel", headers=headers
                ) as resp:
                    cancelled_ok = resp.status == 200
                print("  → 命令已开跑，发出 cancel", flush=True)
            if status in TERMINAL:
                break
            await asyncio.sleep(1.0)
        check(cancelled_ok, "cancel 接口 200")
        check(status == "cancelled", f"任务进入 cancelled（实际 {status}）")
        await _let_sessions_die()

    await runner.cleanup()
    return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
