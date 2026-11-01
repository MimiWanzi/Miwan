"""群聊协调大脑 × 真实全链验证（烧额度：2 个小成员任务 + 十几次 LLM 调用）。

零额度探针（scratch/check_group_brain.py）用假 LLM 端点 + 假成员回放了循环的
全部机械行为；这里关掉的是它们盖不住的那段——**真 LLM 拿着这五个工具，
能不能把一件真事办成**：理解目标 → 拆成两单 → 真派给两个真 CLI Agent →
等收 → 把两人的真实产出收口成总结。

  A  拆解与派单——恰好两条派单卡、两位成员各领一题（大脑不该把活全塞给
     一个人，也不该多派）
  B  成员真跑——两条 agent_result 都到、成员最终回复里带各自的答案
  C  收口汇总——finish 总结里同时出现两个答案（成员产出 → 大脑 → 用户的
     完整数据流，这是"群聊"区别于"任务列表"的那一步）
  D  纪律收尾——群回 idle、任务恰好两个且全 completed、时间线没有甩锅的
     系统消息

前置条件（缺了就 SKIP，退出码 2）：
  1. 协调大脑密钥：config.toml [group] 节（api_key 必填；base_url / model
     可选，默认 DeepSeek），或环境变量 REMOTE_AGENT_GROUP_API_KEY
     （比配置文件优先，base_url / model 同理带 _BASE_URL / _MODEL 后缀）
  2. 本机至少两个 ready 的 Agent（自动取前两个）

额度成本：两次微小的成员任务（纯口算，不碰文件不跑命令）+ 一轮十几次
DeepSeek 调用，约几分钱人民币。协调器一次失败的循环不重试——看时间线上
的系统消息排查，改完再跑。
"""

import asyncio
import json
import os
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

TOKEN = "verify-group-1234567890"
HARD_DEADLINE = 1200.0  # 真成员 + 真 LLM：20 分钟是宽上限，正常几分钟
POLL_TICK = 3.0

QUESTION_1 = "137 × 3 = ?"   # 411
QUESTION_2 = "144 + 357 = ?"  # 501
GOAL = (
    "这是群聊协调大脑的真实验收。请把下面两道算术题各派一个任务，"
    f"两位成员各领一题：题一「{QUESTION_1}」、题二「{QUESTION_2}」。"
    "只派这两个任务，不要派别的。每个任务的 prompt 里写明："
    "直接心算出答案，把算式和答案写在最终回复里，不要执行任何命令。"
    "两个任务都完成后，用 finish 把两个答案（写全数字）汇总给我。"
)

TERMINAL = ("completed", "failed", "cancelled", "disconnected")
# 时间线上出现这些系统消息 = 循环异常收场，早退比等满 20 分钟诚实
BAD_SYSTEM_MARKS = ("协调器调用失败", "轮数上限", "意外出错")

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((ok, what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}", flush=True)


async def dump_timeline(session, base, headers, group_id) -> list[dict]:
    async with session.get(
        f"{base}/api/groups/{group_id}/events", headers=headers
    ) as resp:
        return (await resp.json()).get("events", [])


def print_timeline(events: list[dict]) -> None:
    print("  ---- 群时间线 ----", flush=True)
    for ev in events:
        p = ev.get("payload") or {}
        kind = p.get("kind", ev.get("type"))
        text = str(p.get("text") or p.get("prompt") or "")[:90].replace("\n", " ")
        who = {"user": "用户", "coordinator": "队长", "system": "系统",
               "dispatch": f"派单→{p.get('agent_id')}",
               "agent_result": f"产出·{p.get('agent_id')}({p.get('status')})"}.get(kind, kind)
        print(f"    [{who}] {text}", flush=True)
    print("  ----------------", flush=True)


def kinds(events: list[dict]) -> list[str]:
    return [(e.get("payload") or {}).get("kind", "") for e in events]


def kind_of(e: dict) -> str:
    return (e.get("payload") or {}).get("kind", "")


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ra-verify-group-"))
    try:
        return await _run(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


async def _run(tmp: Path) -> int:
    config = load_config()

    # 协调大脑密钥：环境变量 > config.toml [group]。都没有就 SKIP——
    # 这一项没有免费替代，硬跑只会得到"协调器调用失败"。
    group_key = os.environ.get("REMOTE_AGENT_GROUP_API_KEY", "").strip() or config.group_api_key
    group_base = os.environ.get("REMOTE_AGENT_GROUP_BASE_URL", "").strip() or config.group_base_url
    group_model = os.environ.get("REMOTE_AGENT_GROUP_MODEL", "").strip() or config.group_model
    if not group_key:
        print("SKIP：没有协调大脑密钥。在配置文件加 [group] api_key=…，"
              "或 export REMOTE_AGENT_GROUP_API_KEY=… 后再跑。")
        return 2

    # 端口先选定再进 config（prompt_base_url 按 config.port 拼，同 verify_qoder 的教训）
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    config = replace(
        config, data_dir=tmp / "data", token=TOKEN, port=port,
        group_api_key=group_key, group_base_url=group_base, group_model=group_model,
    )

    app = build_app(config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"X-Token": TOKEN}
    print(f"网关起在 {base}（隔离数据目录）；大脑 {group_model} @ {group_base}", flush=True)

    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True)
    ) as session:
        # 成员：本机 ready 的 Agent 取前两个。少于两个没得分工，SKIP。
        async with session.get(f"{base}/api/agents", headers=headers) as resp:
            agents = (await resp.json())["agents"]
        ready = [a["id"] for a in agents if a.get("status") == "ready"]
        if len(ready) < 2:
            print(f"SKIP：ready 的 Agent 不足两个（{ready}），没法验证分工。")
            await runner.cleanup()
            return 2
        members = ready[:2]
        print(f"成员：{members}", flush=True)

        async with session.post(
            f"{base}/api/projects", headers=headers,
            json={"path": str(tmp / "proj"), "create": True},
        ) as resp:
            project_id = (await resp.json())["project"]["id"]
        async with session.post(
            f"{base}/api/projects/{project_id}/groups", headers=headers,
            json={"title": "群聊验收", "members": members},
        ) as resp:
            assert resp.status == 201, f"建群失败：{resp.status}"
            group_id = (await resp.json())["group"]["id"]
        print(f"群 {group_id} 已建，发目标…", flush=True)

        async with session.post(
            f"{base}/api/groups/{group_id}/messages", headers=headers,
            json={"text": GOAL},
        ) as resp:
            assert resp.status == 200, f"发言失败：{resp.status}"
            woken = (await resp.json()).get("woken")
        print(f"已发言（woken={woken}），等协调循环收场（上限 {int(HARD_DEADLINE)}s）…", flush=True)

        events: list[dict] = []
        status = "?"
        deadline = time.monotonic() + HARD_DEADLINE
        aborted = ""
        while time.monotonic() < deadline:
            events = await dump_timeline(session, base, headers, group_id)
            async with session.get(f"{base}/api/groups/{group_id}", headers=headers) as resp:
                status = (await resp.json())["group"]["status"]
            bad = [
                (e.get("payload") or {}).get("text", "")
                for e in events
                if (e.get("payload") or {}).get("kind") == "system"
                and any(m in (e.get("payload") or {}).get("text", "") for m in BAD_SYSTEM_MARKS)
            ]
            if bad:
                aborted = bad[0]
                break
            if status == "idle":
                break
            await asyncio.sleep(POLL_TICK)

        print(f"循环结束：群状态 {status}"
              + (f"（异常收场：{aborted}）" if aborted else ""), flush=True)
        events = await dump_timeline(session, base, headers, group_id)
        print_timeline(events)

        if aborted or status != "idle":
            check(False, f"协调循环正常收场（实际 {status}）{aborted}")
            return _finish()

        # ---- A：拆解与派单 ----
        dispatches = [(e.get("payload") or {}) for e in events if kind_of(e) == "dispatch"]
        check(len(dispatches) == 2, f"恰好两条派单卡（实际 {len(dispatches)}）")
        dispatched_to = {d.get("agent_id") for d in dispatches}
        check(dispatched_to == set(members),
              f"两位成员各领一题（实际 {sorted(x or '?' for x in dispatched_to)}，期望 {members}）")

        # ---- B：成员真跑 ----
        async with session.get(f"{base}/api/groups/{group_id}", headers=headers) as resp:
            tasks = (await resp.json())["tasks"]
        check(len(tasks) == 2, f"本群恰好两个任务（实际 {len(tasks)}）")
        check(all(t["status"] == "completed" for t in tasks),
              f"任务全 completed（实际 {[t['status'] for t in tasks]}）")
        result_texts = " \n ".join(
            str((e.get("payload") or {}).get("text") or "")
            for e in events if kind_of(e) == "agent_result"
        )
        check("411" in result_texts and "501" in result_texts,
              "成员的最终回复里有各自的答案（411 / 501）")
        results_payloads = [(e.get("payload") or {}) for e in events if kind_of(e) == "agent_result"]
        check(len(results_payloads) == 2 and all(r.get("status") == "completed" for r in results_payloads),
              f"两条 agent_result 都到且 completed（实际 {len(results_payloads)}）")

        # ---- C：收口汇总 ----
        coordinator_texts = [
            str((e.get("payload") or {}).get("text") or "")
            for e in events if kind_of(e) == "coordinator"
        ]
        summary_ok = any("411" in t and "501" in t for t in coordinator_texts)
        check(summary_ok, "收口总结同时含两个答案（产出→大脑→用户数据流贯通）")
        if not summary_ok:
            print("  [诊断] 队长的全部发言：", flush=True)
            for t in coordinator_texts:
                print(f"    · {t[:160]}", flush=True)

        # ---- D：纪律收尾 ----
        check(not aborted, "时间线没有异常系统消息")

    await runner.cleanup()
    return _finish()


def _finish() -> int:
    print("=" * 60)
    passed = sum(1 for ok, _ in results if ok)
    print(f"  {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
