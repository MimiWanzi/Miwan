"""Codex 接入包（OpenAI Codex CLI）。

模块分工：

    locate.py      这台机器上 CLI 在哪（config/PATH/Desktop 内置）、
                   会话存储在哪、子进程环境怎么清
    discovery.py   扫 ~/.codex/sessions 的 rollout JSONL 得到项目与会话
    session.py     驱动进程：每回合一个 codex exec，JSONL 事件归一化
    adapter.py     组装成 AgentAdapter

与 Claude/WorkBuddy 的 Anthropic 方言不同族，解析器是子包自己的
（session.py 的 _Stream）；协议事实（0.162.0-alpha.2 实测）记在
docs/adapter-extension-notes.md 的 Codex 节。
"""

from .adapter import CAPABILITIES, CodexAdapter

__all__ = ["CAPABILITIES", "CodexAdapter"]
