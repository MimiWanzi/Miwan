"""WorkBuddy 接入包（内置的 CodeBuddy Code CLI）。

模块分工：

    locate.py      这台机器上 CLI 在哪（config/PATH/注册表/默认目录）、
                   会话存储在哪、子进程环境怎么清
    discovery.py   扫 ~/.codebuddy/projects 得到项目与会话（CodeBuddy 的
                   JSONL 记录格式，毫秒时间戳）
    session.py     驱动进程：启动、发消息、读事件、中断（deny-only 权限）
    adapter.py     组装成 AgentAdapter

stream-json 解析器与 Claude 共用：adapters/anthropic_stream.py。
"""

from .adapter import CAPABILITIES, WorkBuddyAdapter

__all__ = ["CAPABILITIES", "WorkBuddyAdapter"]
