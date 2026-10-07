"""Claude Code 接入包。

模块分工：

    locate.py      这台机器上 claude 在哪、项目存储在哪、子进程环境怎么清
    discovery.py   扫 ~/.claude/projects 得到项目与会话
    （stream-json 解析器在 adapters/anthropic_stream.py，与 CodeBuddy 共用）
    session.py     驱动进程：启动、发消息、读事件、中断
    adapter.py     组装成 AgentAdapter
"""

from .adapter import CAPABILITIES, ClaudeCodeAdapter

__all__ = ["ClaudeCodeAdapter", "CAPABILITIES"]
