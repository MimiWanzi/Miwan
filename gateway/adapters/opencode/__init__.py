"""OpenCode 接入。

驱动方式和 Claude Code 完全不同：OpenCode 不是"我们拉起子进程、从 stdout
读事件流"，而是**先起一个它自己的 HTTP 服务**（``opencode serve``），然后
所有交互都走这个服务的 v1 API——建会话、发一轮（阻塞到本轮结束）、中断、
SSE 事件流。实测（2026-10-06，版本 1.18.28 源码版）：

- ``POST /session?directory=…``          建会话，可带会话级权限规则集
- ``POST /session/:id/message``          发一轮，**阻塞**到本轮结束，响应即终态消息
- ``POST /session/:id/abort``            中断当前轮，阻塞中的 message 立刻返回
- ``GET  /event?directory=…``            SSE 事件流（按目录作用域）
- ``GET  /session/:id/message``          历史消息（会话历史 UI 用，暂未接）

设计依据（探针结论，写下来免得重新调研）：

- **权限**：规则的求值语义是"最后匹配者胜"，且会话级规则排在 agent 内置
  规则**之后**——所以我们可以用会话规则覆盖内置的 ask。默认 agent 有三个
  会挂起等人的点（doom_loop、访问工作区外目录、读 .env），全部在
  :data:`COMMON_RULES` 里压死，否则 :meth:`send` 会逻辑性死等。
- **deny 的机制**：``pattern="*"`` 的 deny 会把工具从模型视野里**整个摘掉**
  （结构性不可用，最可靠）；窄 pattern 的 deny 才是逐次拒绝。
- **事件**：``message.part.updated`` 携带 part 的最新快照（文本是累积全文、
  工具有 state.status），``message.updated`` 带 ``time.completed``——
  文本在消息完成时发一条，工具按状态机各发一条。
"""

from .adapter import OpenCodeAdapter

__all__ = ["OpenCodeAdapter"]
