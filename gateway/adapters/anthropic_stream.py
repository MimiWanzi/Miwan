"""Anthropic 风格 stream-json 协议 → 归一化事件。

**这是协议解析器，不属于任何一个 Agent。** Claude Code 与 CodeBuddy（经实测：
2.156.0，其 system/init、assistant、user、result 记录与本模块处理的形状一致）
都说这套方言，所以两个 Adapter 共用这里的 Mapper，而不是各抄一份漂移。

顶层 4 种 type：

    system     subtype=init            → task_started
               subtype=其他            → 丢弃（status/thinking_tokens 等，见下）
    assistant  content.text            → assistant_text
               content.tool_use        → tool_call
               content.thinking        → 丢弃
    user       content.tool_result     → tool_result（注意：工具结果在 user 消息里）
    result     subtype=success/error   → task_completed / task_failed

两条硬性要求都由实测得出（Claude 探针 P1，原始样本 probes/out/p1.jsonl）：

  1. **丢弃计数型 system 记录**：thinking_tokens 一类只有 token 计数，
     不丢的话事件流 83% 是噪音。CodeBuddy 还会发 subtype=status（内容为
     null）与 file-history-snapshot，同样零信息量，一并按噪音处理。
     各 Adapter 有专属噪音时，在自己的 session 里先滤掉再喂进来。

  2. **保留 parent_tool_use_id**：主线程为 null，子 Agent 为发起它的
     tool_use.id。不带这个字段，子 Agent 的输出会串进主线，UI 上表现为
     "助手说的话突然接不上"。
"""

from __future__ import annotations

import json
from typing import Any

from ..events import EventType

#: 单个工具结果最多保留多少字符。工具结果经常是整个文件内容，
#: 全量推送会让事件表爆炸、手机流量也不划算。
TOOL_RESULT_LIMIT = 4000

#: 零信息量的 system 子类型——纯粹是计数/状态心跳。
NOISE_SYSTEM_SUBTYPES = frozenset({"thinking_tokens", "status"})

#: 零信息量的顶层记录类型（会话文件的快照/撤销簿之类的伴随流）。
NOISE_TOP_TYPES = frozenset({"file-history-snapshot"})


def flatten_content(content: Any) -> str:
    """把 tool_result 的 content 拍平成字符串。

    它可能是字符串，也可能是 block 列表。非文本内容（如图片）转成 JSON 摘要，
    保证不丢信息也不崩。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif block.get("type") == "image":
                    parts.append(f"[图片 {block.get('source', {}).get('media_type', '?')}]")
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
        text = "\n".join(p for p in parts if p)
    else:
        text = json.dumps(content, ensure_ascii=False)

    if len(text) > TOOL_RESULT_LIMIT:
        dropped = len(text) - TOOL_RESULT_LIMIT
        text = f"{text[:TOOL_RESULT_LIMIT]}\n…（已截断 {dropped} 字符）"
    return text


class Mapper:
    """有状态地把一个会话的原始记录翻译成归一化事件。

    之所以有状态：``system/init`` 里才有 model / 工具集 / 真实 session_id，
    而 ``task_started`` 事件要带上它们，所以得先记住、等 init 到了再发。
    """

    def __init__(self) -> None:
        self.native_session_id: str | None = None
        self._turn_prompt: str | None = None
        self._started_this_turn = False

    def begin_turn(self, prompt: str) -> None:
        """一轮开始。注意**不清空** native_session_id——它是跨轮保持的。"""
        self._turn_prompt = prompt
        self._started_this_turn = False

    def end_turn(self) -> None:
        self._turn_prompt = None

    def handle(self, record: dict) -> list[tuple[EventType, dict]]:
        """一条原始记录 → 若干归一化事件。认不出来的一律降级为 raw，不丢信息。"""
        kind = record.get("type")

        if kind in NOISE_TOP_TYPES:
            return []
        if kind == "system":
            return self._system(record)
        if kind == "assistant":
            return self._assistant(record)
        if kind == "user":
            return self._user(record)
        if kind == "result":
            return []  # 由 outcome() 处理，它还要算成败
        return [(EventType.RAW, {"reason": "未知顶层类型", "record": record})]

    # -- 各类型 --------------------------------------------------------

    def _system(self, record: dict) -> list[tuple[EventType, dict]]:
        subtype = record.get("subtype")

        if subtype in NOISE_SYSTEM_SUBTYPES:
            return []

        if subtype != "init":
            return [(EventType.RAW, {"reason": f"未知 system 子类型 {subtype}", "record": record})]

        session_id = record.get("session_id")
        if session_id:
            self.native_session_id = session_id

        # init 只在一轮开始时出现，据此保证 task_started 每轮恰好一条
        if self._started_this_turn:
            return []
        self._started_this_turn = True

        return [
            (
                EventType.TASK_STARTED,
                {
                    "prompt": self._turn_prompt,
                    "cwd": record.get("cwd"),
                    "model": record.get("model"),
                    "tools": record.get("tools"),
                    "native_session_id": self.native_session_id,
                },
            )
        ]

    def _assistant(self, record: dict) -> list[tuple[EventType, dict]]:
        parent = record.get("parent_tool_use_id")
        message = record.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            return []

        events: list[tuple[EventType, dict]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    events.append(
                        (EventType.ASSISTANT_TEXT, {"text": text, "parent_tool_use_id": parent})
                    )
            elif btype == "tool_use":
                events.append(
                    (
                        EventType.TOOL_CALL,
                        {
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input"),
                            "parent_tool_use_id": parent,
                        },
                    )
                )
            elif btype == "thinking":
                # 丢思考内容：原型书的事件集里没有它的位置，UI 上无处安放，
                # 而且多数情况下库里只存了签名、正文是空的。
                continue
            else:
                events.append(
                    (EventType.RAW, {"reason": f"未知 content 类型 {btype}", "block": block})
                )
        return events

    def _user(self, record: dict) -> list[tuple[EventType, dict]]:
        """工具结果藏在 user 消息里——这是这套流格式里最反直觉的一点。"""
        parent = record.get("parent_tool_use_id")
        message = record.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            return []

        events: list[tuple[EventType, dict]] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                events.append(
                    (
                        EventType.TOOL_RESULT,
                        {
                            "tool_use_id": block.get("tool_use_id"),
                            "content": flatten_content(block.get("content")),
                            "is_error": bool(block.get("is_error")),
                            "parent_tool_use_id": parent,
                        },
                    )
                )
        return events

    # -- 终态 ----------------------------------------------------------

    def outcome(self, result: dict) -> tuple[bool, str | None, list[tuple[EventType, dict]]]:
        """从 result 消息判定成败，并产出终态事件。

        返回 ``(ok, reason, events)``。

        **不看进程退出码**：实测 claude 任务成功时退出码也可能是 1
        （stderr 报 unrecognized_model，但那不影响任务完成）。
        退出码反映 CLI 自身状态，result.subtype 才反映任务状态。
        """
        subtype = result.get("subtype") or ""
        is_error = bool(result.get("is_error"))

        session_id = result.get("session_id")
        if session_id:
            self.native_session_id = session_id

        # 无人应答的权限提示会以"事后拒绝"的形式出现在这里，而不是以一个
        # 待批准的请求出现。对平台来说这是"一个权限决策点发生了，结果是拒绝"——
        # 第 6 层实现权限通道后，同一个事件类型会承载"等人批准"的版本。
        events: list[tuple[EventType, dict]] = []
        for denial in result.get("permission_denials") or []:
            if not isinstance(denial, dict):
                continue
            events.append(
                (
                    EventType.PERMISSION_REQUEST,
                    {
                        "tool_name": denial.get("tool_name"),
                        "tool_input": denial.get("tool_input"),
                        "decision": "deny",
                        # 当前没有权限通道，操作被自动拒绝（而非等人批准后拒绝）
                        "source": "no_host",
                    },
                )
            )

        ok = (not is_error) and subtype == "success"
        payload = {
            "reason": subtype or ("error" if is_error else "completed"),
            "is_error": is_error,
            "cost_usd": result.get("total_cost_usd"),
            "duration_ms": result.get("duration_ms"),
            "num_turns": result.get("num_turns"),
        }
        if not ok:
            # 错误文本的字段各家不同：Claude 在 result/error，CodeBuddy 是
            # errors 数组（实测 2.156.0：认证失败时为 ["Authentication required…"]）
            error = result.get("result") or result.get("error")
            if not error and isinstance(result.get("errors"), list):
                error = "；".join(str(e) for e in result["errors"] if e)
            payload["error"] = error
            events.append((EventType.TASK_FAILED, payload))
        else:
            events.append((EventType.TASK_COMPLETED, payload))

        return ok, subtype or None, events
