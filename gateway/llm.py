"""OpenAI 兼容的 chat completions 客户端——群聊协调大脑的调用面。

aiohttp 直连、不引 openai SDK：这个项目的依赖面就是 aiohttp + pydantic
（CLAUDE.md 速查），为一个每分钟几次的调用引入整个 SDK 不划算。

只实现协调器用到的那一小块，**刻意不做的事**：

  - 流式：队长的话一次拿全，群时间线不是逐字渲染的聊天打字机
  - 重试：失败如实上抛，由协调循环变成群里的系统消息——
    对用户诚实比悄悄重试三次更省心（重试烧的是真钱）
  - 多模态 / json 模式 / logprobs：没有协调器用得上的场景就不接

base_url 指 OpenAI 兼容端点的根（如 ``https://api.deepseek.com``），
路径按惯例拼 ``/chat/completions``。需要 ``/v1`` 前缀的服务商（如某些
自建网关）直接把前缀写进 base_url，这里不做路径猜测——猜错了比报错难查。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import aiohttp

__all__ = ["ChatResult", "LLMError", "LLMClient", "ToolCall"]


class LLMError(RuntimeError):
    """调用失败（网络 / 非 200 / 响应形状不对）。message 面向用户展示。"""


@dataclass
class ToolCall:
    """一条工具调用。arguments 原样保留 JSON 串——解析是协调器的事，
    这里不猜模型给的参数形状对不对。"""

    id: str
    name: str
    arguments: str


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[ToolCall]
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class LLMClient:
    """一个配置一份客户端。无状态：并发调用安全。"""

    base_url: str
    api_key: str
    model: str
    timeout: float = 120.0

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> ChatResult:
        body: dict = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
            # auto 是默认值，显式写出来是为了行为不依赖服务商的默认差异
            body["tool_choice"] = "auto"

        url = f"{self.base_url}/chat/completions"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise LLMError(
                            f"LLM 端点返回 {resp.status}：{text[:300]}"
                        )
                    try:
                        data = json.loads(text)
                    except ValueError as exc:
                        raise LLMError(f"LLM 响应不是 JSON：{text[:200]}") from exc
        except aiohttp.ClientError as exc:
            raise LLMError(f"LLM 端点连不上：{type(exc).__name__}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise LLMError(f"LLM 端点 {self.timeout:g}s 没有响应") from exc

        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> ChatResult:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"LLM 响应缺少 choices：{json.dumps(data)[:200]}")
        message = choices[0].get("message") or {}

        tool_calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            tool_calls.append(
                ToolCall(
                    id=str(raw.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    arguments=str(fn.get("arguments") or ""),
                )
            )
        usage = data.get("usage") or {}
        return ChatResult(
            content=message.get("content"),
            tool_calls=tool_calls,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
