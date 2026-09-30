#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 Adapter 实现本身——不是验证 CLI。

probe_claude.py 回答的是"Claude Code 能不能做某事"；这个脚本回答的是
"**我写的那层适配代码有没有把这件事做对**"。两者是不同的风险：

  - CLI 支持双工 ≠ 我的 Mapper 把轮次切对了
    （如果 result 不是一轮的最后一个事件，事件就会串到下一轮去）
  - CLI 支持 --resume ≠ resume 与 stream-json 输入**组合**起来能用
    （这个组合探针没测过，只在中断重起时才走到）

用法：
    python probes/verify_adapter.py

成本：约 0.05 美元（4 轮，其中 1 轮被中途掐断）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.adapters.base import AutonomyPolicy  # noqa: E402
from gateway.adapters.claude_code import ClaudeCodeAdapter  # noqa: E402
from gateway.events import Event, EventType  # noqa: E402

SCRATCH = Path(__file__).resolve().parent / "scratch"
MAGIC = "4271"

#: 故意选一个产字多的提示词，好让中断有机会在它结束前插进去。
#: 用"数到 5000"之类会更稳，但 token 烧得没必要。
LONG_PROMPT = "请用中文写一段 800 字左右的说明，介绍 Python 的 GIL 是什么、它解决了什么问题。"


class Recorder:
    """收集事件，同时按轮分组打印。"""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.turns: list[list[Event]] = []

    def start_turn(self) -> None:
        self.turns.append([])

    def __call__(self, event: Event) -> None:
        self.events.append(event)
        if self.turns:
            self.turns[-1].append(event)

    def texts(self, turn: int = -1) -> str:
        return " ".join(
            str(e.payload.get("text", ""))
            for e in self.turns[turn]
            if e.type is EventType.ASSISTANT_TEXT
        )


def show(label: str, events: list[Event]) -> None:
    print(f"\n  ── {label} " + "─" * max(0, 60 - len(label)))
    for event in events:
        payload = event.payload
        if event.type is EventType.TASK_COMPLETED:
            detail = f"reason={payload.get('reason')} cost={payload.get('cost_usd')}"
        elif event.type is EventType.TASK_FAILED:
            detail = f"reason={payload.get('reason')}"
        else:
            detail = ""
        indent = "    └ " if payload.get("parent_tool_use_id") else ""
        text = str(payload.get("text") or payload.get("name") or "").replace("\n", " ")[:60]
        print(f"     {event.type.value:<18}{indent}{detail} {text}")


async def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)

    adapter = ClaudeCodeAdapter()
    info = adapter.detect()
    if not info.ready:
        print(f"Claude Code 不可用：{info.detail}")
        return 1
    print(f"Agent  {info.name} {info.version}")

    recorder = Recorder()
    session = adapter.create_session(
        cwd=SCRATCH,
        task_id="verify-adapter",
        emit=recorder,
        autonomy=AutonomyPolicy.READ_ONLY,
    )

    checks: list[tuple[str, bool]] = []

    try:
        # 1) 第一轮：写入记忆
        recorder.start_turn()
        out1 = await session.send(f"请记住数字 {MAGIC}。只回复 OK。")
        show("第 1 轮 · 写入记忆", recorder.turns[-1])
        checks.append(("第 1 轮成功", out1.ok))
        checks.append(("第 1 轮拿到 session id", bool(out1.native_session_id)))

        # 2) 第二轮：同一进程内追问，验证轮次没串
        recorder.start_turn()
        out2 = await session.send("我刚才让你记住的数字是多少？只回复这个数字。")
        show("第 2 轮 · 同进程追问", recorder.turns[-1])
        checks.append(("第 2 轮成功", out2.ok))
        checks.append(("同进程跨轮上下文保持", MAGIC in recorder.texts()))
        checks.append(
            ("第 2 轮恰好 1 个终态事件",
             sum(1 for e in recorder.turns[-1]
                 if e.type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED)) == 1)
        )

        # 3) 第三轮：中途掐断
        recorder.start_turn()
        pending = asyncio.create_task(session.send(LONG_PROMPT))
        await asyncio.sleep(2.5)
        await session.interrupt()
        out3 = await pending
        show("第 3 轮 · 中途中断", recorder.turns[-1])
        checks.append(("中断被识别为 cancelled（而非崩溃）", out3.reason == "cancelled"))

        # 4) 第四轮：中断后重起，验证 --resume + stream-json 的组合
        recorder.start_turn()
        out4 = await session.send("我刚才让你记住的数字是多少？只回复这个数字。")
        show("第 4 轮 · 中断后重起", recorder.turns[-1])
        checks.append(("中断后重起的轮次成功", out4.ok))
        checks.append(("重起后上下文仍保持（resume 生效）", MAGIC in recorder.texts()))

    finally:
        await session.close()

    # 5) 事件流里不该有噪音
    checks.append((
        "事件流无可识别的噪音",
        all(e.type is not EventType.RAW for e in recorder.events),
    ))

    print("\n" + "=" * 66)
    failed = 0
    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'}  {label}")
        failed += 0 if ok else 1
    print("=" * 66)
    print(f"  共 {len(recorder.events)} 个归一化事件，{len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
