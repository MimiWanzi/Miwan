"""Adapter 注册表。

核心层通过这里拿到 Agent，**永远不 import ``adapters.claude_code`` 这类具体子包**。
新增一个 Agent 的完整步骤：

    1. 新建 ``adapters/<agent>/`` 子包，实现 AgentAdapter 的四个成员
    2. 在这里 import 它的 Adapter 类并调用 register()
    3. 完。runner / api / ws / store 一行都不用改。

Codex 和 OpenCode 尚未实现。它们与 Claude Code 的形态差别（每轮一个进程 /
HTTP 服务）已经被抽象层容纳，但**没有被实测验证过**——
真去接的时候要重新走一遍探针，别把这里的接口当成"已经证明可行"。
"""

from __future__ import annotations

import sys

from .base import (
    AgentAdapter,
    AgentCapabilities,
    AgentInfo,
    AgentSession,
    AgentStatus,
    AutonomyPolicy,
    BlockFilter,
    DEFAULT_AUTONOMY,
    EmitFn,
    RunOutcome,
    ScanResult,
    normalize_path,
    stable_id,
)

_REGISTRY: list[type[AgentAdapter]] = []


def register(adapter_cls: type[AgentAdapter]) -> type[AgentAdapter]:
    """注册一个 Adapter。重复注册同一个 agent_id 会直接报错——

    静默覆盖会让"到底哪个实现在跑"变成一个需要翻代码才能回答的问题。
    """
    existing = {cls.agent_id for cls in _REGISTRY}
    if adapter_cls.agent_id in existing:
        raise ValueError(f"agent_id 重复：{adapter_cls.agent_id}")
    _REGISTRY.append(adapter_cls)
    return adapter_cls


def registered() -> list[type[AgentAdapter]]:
    return list(_REGISTRY)


def get(agent_id: str) -> type[AgentAdapter] | None:
    for cls in _REGISTRY:
        if cls.agent_id == agent_id:
            return cls
    return None


def detect_all() -> list[AgentInfo]:
    """检测所有已注册的 Agent。

    单个 Adapter 检测失败不能拖垮其余的——手机上看到"Codex 检测异常"
    远比"整个网关起不来"有用。失败时降级成 UNCONFIGURED 并带上原因。
    """
    infos: list[AgentInfo] = []
    for cls in _REGISTRY:
        try:
            infos.append(cls.detect())
        except Exception as exc:  # noqa: BLE001 —— 刻意兜底，见上
            infos.append(
                AgentInfo(
                    id=cls.agent_id,
                    name=cls.display_name,
                    status=AgentStatus.UNCONFIGURED,
                    detail=f"检测时抛出异常：{type(exc).__name__}: {exc}",
                )
            )
    return infos


def ready_adapters() -> list[AgentAdapter]:
    """所有可用的 Adapter 实例。"""
    return [cls() for cls in _REGISTRY if _is_ready_safe(cls)]


def _is_ready_safe(cls: type[AgentAdapter]) -> bool:
    try:
        return cls.detect().ready
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------
# 注册具体实现
# ----------------------------------------------------------------------

from .claude_code import ClaudeCodeAdapter  # noqa: E402

register(ClaudeCodeAdapter)

#: 尚未实现，列在这里是为了让"扩展路径"可见而不是只存在于文档里。
#: 接 Codex 时在这里 register(CodexAdapter) 即可。
PENDING_AGENTS = ("codex", "opencode", "cursor")

if sys.version_info < (3, 12):  # pragma: no cover
    # 用到了 asyncio.timeout（3.11+）与 3.12 的若干写法，早点报错比运行到一半崩好
    raise RuntimeError("Gateway 需要 Python 3.12+")

__all__ = [
    "AgentAdapter",
    "AgentCapabilities",
    "AgentInfo",
    "AgentSession",
    "AgentStatus",
    "AutonomyPolicy",
    "BlockFilter",
    "DEFAULT_AUTONOMY",
    "EmitFn",
    "PENDING_AGENTS",
    "RunOutcome",
    "ScanResult",
    "detect_all",
    "get",
    "normalize_path",
    "ready_adapters",
    "register",
    "registered",
    "stable_id",
]
