"""Codex CLI × 真 CLI 验证（烧额度：五六个小任务）。

零额度探针（scratch/check_codex_core.py）验证的是网关这一半（桩 CLI +
假 rollout）；这里验证**真 CLI** 全链，把协议捕获后剩余的疑问关掉：

  A（read_only）纯文本任务——JSONL 全链、thread_id 落库、token usage、
    rollout 真的落盘（discovery 的 read_history 能按 uuid 读到它）
  B（edit）写文件——apply_patch 走 file_change 事件、workspace-write
    沙箱生效（文件真的出现在磁盘上）
  C（resume_from=A）续聊——exec resume 跨进程上下文保持（问 A 里埋的暗号）
  D（read_only）撞写——read-only 沙箱拒写：任务必须走到终态不挂死
    （permission_gate=False 的档位验证，写动作要么被沙箱拒、要么失败）
  E（full）真跑命令——danger-full-access 下 shell 命令零拒绝执行，
    tool_result 内嵌标记串防"假绿"（同 verify_workbuddy 的 E 节教训）

前置条件：Codex CLI 可被找到（Desktop 内置/PATH/config [codex] command），
且 ~/.codex/config.toml 里的模型后端可用（直连 API 或 Desktop 代理在线）。
后端不通时 A 节会以连接类错误失败——探针识别为 SKIP（exit 2）。

**工作区刻意放 probes/scratch 而不是 %TEMP%**：0.162 Windows 沙箱（unelevated）
对 tempfile.mkdtemp 建的目录连读都 EPERM（实测：workspace-write 下 apply_patch、
shell、node_repl 全被拒；同一策略下仓库路径与 %TEMP% 根目录正常），放 temp
会让 B 节假失败。scratch 同时在 default_blocked_paths 里，不会污染项目发现。

额度成本：几个小任务对话，约几分钱。
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from gateway.adapters.codex import discovery  # noqa: E402
from gateway.adapters.codex.locate import sessions_dir  # noqa: E402
from gateway.config import load_config  # noqa: E402
from gateway.server import build_app  # noqa: E402

TOKEN = "verify-codex-1234567890"
HARD_PER_TASK = 300.0
TERMINAL = ("completed", "failed", "cancelled", "disconnected")

#: 模型后端不通/认证失效的特征——这是环境问题不是网关缺陷，按 SKIP 处理
#: （人类提示语见 A 节失败分支）。指一直没验证过 turn.failed 的具体文本，
#: 靠关键词近似，宁可误 SKIP 也不误 FAIL。
ENV_FAILURE_MARKERS = (
    "connect", "connection", "refused", "timed out", "timeout", "unreachable",
    "401", "403", "unauthorized", "invalid api key", "quota", "余额",
    "provider", "stream error", "dispatch", "insufficient",
)

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
        "agent_id": "codex",
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
    return json.dumps(p, ensure_ascii=False)[:400] if p else ""


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


async def main() -> int:
    # 见模块 docstring：工作区不能放 %TEMP%（codex Windows 沙箱对 mkdtemp
    # 目录整棵 EPERM），放 scratch——用时间戳保证多次运行互不串场
    tmp = ROOT / "probes" / "scratch" / f"verify-codex-ws-{int(time.time())}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        skipped = await _run(tmp)
    finally:
        await rmtree_with_retry(tmp)
    if skipped:
        return 2
    print("=" * 60)
    passed = sum(1 for ok, _ in results if ok)
    print(f"  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


async def _run(tmp: Path) -> bool:
    """返回 True = SKIP（环境不满足），False = 完整跑完。"""
    # REMOTE_AGENT_CODEX_SESSIONS_DIR 是零额度探针的夹具口，E2E 必须读真目录
    assert "REMOTE_AGENT_CODEX_SESSIONS_DIR" not in os.environ, \
        "E2E 必须读真实的 ~/.codex/sessions，请先清掉该环境变量"
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
            me = next((a for a in agents if a["id"] == "codex"), None)
        if me is None or me.get("status") != "ready":
            print("SKIP：codex 未就绪（{}）".format((me or {}).get("detail")))
            await runner.cleanup()
            return True
        print("codex 就绪：", me.get("version") or me.get("executable"), flush=True)

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
            "请记住暗号 CX-8247。然后只回复：好的。",
            "read_only",
        )
        started = first_payload(events, "task_started")
        check(started is not None, "task_started 到达（turn.started 被归一化）")
        check(status == "completed", f"任务完成（实际 {status}，{int(time.monotonic()-t0)}s）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        if status != "completed" and any(
            m in failed_reason(events).lower() for m in ENV_FAILURE_MARKERS
        ):
            print("SKIP：模型后端不可用（~/.codex/config.toml 的 provider 连不上或认证失效）。\n"
                  f"  失败详情：{failed_reason(events)}")
            await runner.cleanup()
            return True
        texts = [p.get("text", "") for p in payloads_of(events, "assistant_text")]
        check(any("好的" in t for t in texts), f"assistant 文本到达（{texts[:1]}）")
        completed = first_payload(events, "task_completed")
        check(completed is not None and (completed.get("usage") or {}).get("output_tokens"),
              f"token usage 到位（{completed and completed.get('usage')}）")
        native_id = (started or {}).get("native_session_id")
        check(bool(native_id), f"native_session_id 落库（{native_id}）")
        raw_errors = payloads_of(events, "raw")
        if raw_errors:
            print(f"  [记录] RAW 事件 {len(raw_errors)} 条（首条：{json.dumps(raw_errors[0], ensure_ascii=False)[:120]}）")

        if native_id:
            history = discovery.read_history(sessions_dir(), native_id)
            check(any("CX-8247" in m.get("text", "") for m in history or [] if "text" in m),
                  "rollout 真的落盘且 read_history 按 uuid 读得到（持久化契约）")
            if history is not None:
                kinds = sorted({"tool" if "tool" in m else "text" for m in history})
                print(f"  [记录] rollout 条目种类：{kinds}，共 {len(history)} 条")
        else:
            check(False, "没有 native_session_id，持久化契约无法验证")

        # ---- B：edit 写文件 ----
        print("== B：edit 写文件（workspace-write 生效） ==")
        _, events, status = await drive(
            session, base, headers, project_id,
            "在工作目录创建文件 hello.txt，内容就写 hi 两个字母。完成后只回复：写好了。",
            "edit",
        )
        check(status == "completed", f"任务完成（实际 {status}）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        tool_calls = payloads_of(events, "tool_call")
        check(bool(tool_calls),
              f"tool_call 事件到达（{[t.get('name') for t in tool_calls][:3]}）")
        check((tmp / "proj" / "hello.txt").exists(),
              "文件真的出现在磁盘上（workspace-write 沙箱生效）")

        # ---- C：exec resume 续聊 ----
        print("== C：exec resume 续聊上下文 ==")
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
            check(any("CX-8247" in t for t in texts),
                  f"跨进程上下文保持：暗号答对了（{texts[:1]}）")

        # ---- D：read_only 档撞写不挂死 ----
        print("== D：read-only 沙箱拒写不挂死 ==")
        _, events, status = await drive(
            session, base, headers, project_id,
            "在工作目录创建文件 denied.txt，内容写 no。如果被拒绝就回复：被拒了。",
            "read_only",
        )
        check(status in TERMINAL and status != "disconnected",
              f"撞写的任务走到了终态（实际 {status}）——没有挂死")
        check(not (tmp / "proj" / "denied.txt").exists(),
              "read-only 档下文件没有出现（沙箱边界真实存在）")

        # ---- E：FULL 档真跑命令（tool_result 内嵌标记防假绿）----
        print("== E：FULL 档 danger-full-access 生效 ==")
        _, events, status = await drive(
            session, base, headers, project_id,
            "用 shell 运行命令：echo codex-full-ok-2026，然后只回复命令的输出。不要做其他事。",
            "full",
        )
        check(status == "completed", f"任务完成（实际 {status}）"
              + (f"；失败原因 {failed_reason(events)}" if status != "completed" else ""))
        check(tool_output_contains(events, "codex-full-ok-2026"),
              "shell 命令真的执行了（tool_result 含标记串）——FULL 档放行生效")

    await runner.cleanup()
    return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
