"""``read_history`` 消息条目的共享格式。

条目形状（前端 ``renderHistoryMessage`` 按此渲染）：

  - 文本消息：``{"role": "user"|"assistant", "text": str, "ts": float|None}``
  - 工具摘要：``{"role": "assistant", "tool": str, "detail": str|None, "ts": float|None}``

这是 Adapter 之间的契约：格式常量和工具摘要规则放在 adapters 这一层，
各 Adapter 共用一份，改这里两边同时生效，不会各写一份后漂移。
"""

from __future__ import annotations

#: 单条文本的长度上限——手机上放不下，历史页也不该为一次长输出存一页纸。
TEXT_LIMIT = 2000

#: 从工具入参里挑摘要时优先看的字段（与前端 ``ARG_FIELD`` 同思路）。
_DETAIL_FIELDS = (
    "file_path", "notebook_path", "command", "pattern", "path", "url", "query",
)


def tool_detail(input_data: object) -> str | None:
    """从工具入参里挑一行最有信息量的摘要；挑不出返回 None。"""
    if not isinstance(input_data, dict) or not input_data:
        return None
    for key in _DETAIL_FIELDS:
        value = input_data.get(key)
        if value:
            return f"{key}={str(value)[:120]}"
    first = next(iter(input_data.values()))
    return str(first)[:120]
