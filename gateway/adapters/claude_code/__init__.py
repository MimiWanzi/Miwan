"""Claude Code 接入包。

模块分工：

    locate.py      这台机器上 claude 在哪、项目存储在哪、子进程环境怎么清
    discovery.py   扫 ~/.claude/projects 得到项目与会话
    normalize.py   原始 stream-json → 归一化事件（最容易随版本变化，故单独成文件）
    session.py     驱动进程：启动、发消息、读事件、中断
    adapter.py     组装成 AgentAdapter
"""

from .adapter import CAPABILITIES, ClaudeCodeAdapter

__all__ = ["ClaudeCodeAdapter", "CAPABILITIES"]
