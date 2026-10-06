"""OpenCode 会话：一轮 = 一次阻塞的 HTTP 调用 + 一条 SSE 事件流。

POST ``/session/:id/message`` 的响应里只有**终态消息**，过程中的工具调用、
文本都要从 ``GET /event`` 的 SSE 流里拿。所以 send() 的骨架是
"开泵 → 发请求（阻塞到本轮结束）→ 关泵 → 汇总终态"。

泵和请求之间的顺序是刻意的：**泵必须先起**。请求一发出，最初的一批事件
（step-start、第一个工具调用）马上就来，等响应回来再开泵就晚了。

权限的处理思路和 Claude 相反：Claude 是"放行 + 绊线拦截"，OpenCode 是
"会话级规则集**预先压死所有会挂起等人的权限点**"。默认 build agent 有四个
这样的点（读 .env、访问工作区外目录、doom_loop、question），任何一个没压住，
agent 就会在工具循环中间挂起等人——而 :meth:`send` 在等那轮结束，整个任务
就死锁了。规则集里全部 deny 之后，:meth:`_reject_permission` 只是兜底：
万一有漏网的权限点冒出来，立刻拒绝并落到事件上，**绝不干等**。
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from pathlib import Path

import aiohttp

from ...events import EventType
from ...permissions import Policy
from ..base import AgentSession, AutonomyPolicy, EmitFn, RunOutcome
from . import serve

#: 所有档位共用的会话级规则。求值语义是"最后匹配者胜"，且会话级规则排在
#: agent 内置规则之后，所以这些规则能覆盖默认 agent 的 ask。
COMMON_RULES: list[dict[str, str]] = [
    # 默认 agent 对 .env 一类的读会 ask——无人应答等于卡死，必须放行
    {"permission": "read", "pattern": "*", "action": "allow"},
    # doom_loop 检测会在连续重复操作时 ask；压成 deny，让循环直接断掉
    {"permission": "doom_loop", "pattern": "*", "action": "deny"},
    # 访问工作区外的目录默认 ask。手机遥控的场景下工作区就是边界
    {"permission": "external_directory", "pattern": "*", "action": "deny"},
    # question 工具是"agent 反问用户"——没人能答，禁掉让它自己拿主意
    {"permission": "question", "pattern": "*", "action": "deny"},
]

#: pattern="*" 的 deny 会把工具从模型视野里**整个摘掉**（结构性不可用），
#: 所以 READ_ONLY / EDIT 档用它来表达，和 Claude 的限定工具集同一思路。
READ_ONLY_DENY = ("edit", "bash", "task", "todowrite", "skill", "webfetch", "websearch", "lsp")
EDIT_DENY = ("bash", "task", "skill")


def _ruleset(autonomy: AutonomyPolicy, policy: Policy) -> list[dict[str, str]]:
    """把档位 + 绊线翻译成 OpenCode 的会话级权限规则集。

    FULL 档把绊线的 glob 展开成 bash deny 规则——和 Claude 把绊线喂给
    ``--disallowedTools`` 是同一件事的两种方言（见 :class:`Tripwire` 的注释：
    glob 是尽力而为的第二道，权威在正则，而 OpenCode 只认 glob）。
    """
    rules = list(COMMON_RULES)
    denied = {
        AutonomyPolicy.READ_ONLY: READ_ONLY_DENY,
        AutonomyPolicy.EDIT: EDIT_DENY,
        AutonomyPolicy.FULL: (),
    }[autonomy]
    for name in denied:
        rules.append({"permission": name, "pattern": "*", "action": "deny"})
    if autonomy is AutonomyPolicy.FULL:
        for tripwire in policy.tripwires:
            for glob in tripwire.globs:
                rules.append({"permission": "bash", "pattern": glob, "action": "deny"})
    return rules


#: SSE 里工具结果的正文上限，和 Claude 的 :data:`TOOL_RESULT_LIMIT` 对齐——
#: 手机上放不下，库里也不该为一次 bash 输出存一页纸。
TOOL_RESULT_LIMIT = 4000


def _clip(text: str) -> str:
    text = text or ""
    if len(text) > TOOL_RESULT_LIMIT:
        dropped = len(text) - TOOL_RESULT_LIMIT
        return f"{text[:TOOL_RESULT_LIMIT]}\n…（已截断 {dropped} 字符）"
    return text


class OpenCodeSession(AgentSession):
    """一个 OpenCode 会话。serve 进程由全网关共享，这里只管自己的会话 id。"""

    def __init__(
        self,
        *,
        cwd: Path,
        task_id: str,
        emit: EmitFn,
        autonomy: AutonomyPolicy = AutonomyPolicy.READ_ONLY,
        resume_from: str | None = None,
        policy: Policy | None = None,
    ) -> None:
        super().__init__(
            cwd=cwd,
            task_id=task_id,
            emit=emit,
            autonomy=autonomy,
            resume_from=resume_from,
        )
        self.policy = Policy() if policy is None else policy
        self._http: aiohttp.ClientSession | None = None
        self._base: str | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._pump_stop = asyncio.Event()
        self._interrupted = False
        # 每轮重置的现场：messageID → 累积文本、已发过 ASSISTANT_TEXT 的消息、
        # 已发过 TOOL_CALL 的 callID（SSE 是快照流，去重全靠这里）
        self._texts: dict[str, str] = {}
        self._announced: set[str] = set()
        self._tools_announced: set[str] = set()

    # ------------------------------------------------------------------
    # AgentSession 接口
    # ------------------------------------------------------------------

    async def send(self, prompt: str) -> RunOutcome:
        if self._closed:
            raise RuntimeError("会话已关闭")
        self._interrupted = False
        self._texts.clear()
        self._announced.clear()
        self._tools_announced.clear()

        self._http, self._base = await serve.manager.acquire()
        directory = urllib.parse.quote(str(self.cwd), safe="")

        if self.native_session_id is None:
            await self._create_session(directory, prompt)
        sid = self.native_session_id
        assert sid is not None

        self._pump_stop.clear()
        self._pump_task = asyncio.create_task(self._pump_events(directory))

        self._emit(
            EventType.TASK_STARTED,
            {
                "prompt": prompt,
                "cwd": str(self.cwd),
                "model": None,
                "tools": None,
                "native_session_id": sid,
            },
        )

        try:
            async with self._http.post(
                f"{self._base}/session/{sid}/message?directory={directory}",
                json={"parts": [{"type": "text", "text": prompt}], "agent": "build"},
            ) as resp:
                if resp.status != 200:
                    detail = (await resp.text())[:500]
                    raise RuntimeError(f"OpenCode 一轮失败（HTTP {resp.status}）：{detail}")
                reply = await resp.json()
        finally:
            await self._stop_pump()

        await self._flush_texts(reply)
        return self._outcome(reply)

    async def interrupt(self) -> None:
        """中断当前轮：POST abort，阻塞中的 message 调用会立刻返回（finish=None）。"""
        if self._http is None or self._base is None or self.native_session_id is None:
            return
        self._interrupted = True
        directory = urllib.parse.quote(str(self.cwd), safe="")
        try:
            async with self._http.post(
                f"{self._base}/session/{self.native_session_id}/abort?directory={directory}"
            ) as resp:
                await resp.read()
        except aiohttp.ClientError:
            pass

    async def close(self) -> None:
        self._closed = True
        await self._stop_pump()
        # 刻意不 DELETE opencode 侧的会话：历史留着，discover 才能把它列出来，
        # 用户也才能在它自己的 UI 里翻——和 Claude 不删 JSONL 同一个道理。

    # ------------------------------------------------------------------
    # 会话建立
    # ------------------------------------------------------------------

    async def _create_session(self, directory: str, prompt: str) -> None:
        """建 opencode 会话并记下它的会话 id。

        规则集在建会话时一次性交给它，**之后改不了**（v1 没有 update）。
        续接的会话沿用创建时的规则集——换档位续跑时会以旧档位的边界工作，
        这是已知限制，不是因为做不到而忽略。
        """
        assert self._http is not None and self._base is not None
        body = {
            "title": prompt[:80] or "远程任务",
            "permission": _ruleset(self.autonomy, self.policy),
        }
        async with self._http.post(
            f"{self._base}/session?directory={directory}", json=body
        ) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:500]
                raise RuntimeError(f"创建 OpenCode 会话失败（HTTP {resp.status}）：{detail}")
            data = await resp.json()
        session_id = data.get("id") if isinstance(data, dict) else None
        if not session_id:
            raise RuntimeError(f"OpenCode 会话响应里没有 id：{str(data)[:300]}")
        self.native_session_id = session_id

    # ------------------------------------------------------------------
    # SSE 泵
    # ------------------------------------------------------------------

    async def _pump_events(self, directory: str) -> None:
        """持续读 SSE，直到本轮结束（_stop_pump）。

        断线重连是给"轮还没结束但连接断了"准备的：agent 还在干活，丢了
        事件等于丢过程记录。0.5s 的退避足够——真连不上，message 的响应
        返回时整轮照样收场。
        """
        assert self._http is not None and self._base is not None
        url = f"{self._base}/event?directory={directory}"
        while not self._pump_stop.is_set():
            try:
                async with self._http.get(url) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(1.0)
                        continue
                    async for raw in resp.content:
                        if self._pump_stop.is_set():
                            break
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            evt = json.loads(line[5:])
                        except json.JSONDecodeError:
                            continue
                        if isinstance(evt, dict):
                            await self._dispatch(evt)
            except aiohttp.ClientError:
                pass
            if not self._pump_stop.is_set():
                await asyncio.sleep(0.5)

    async def _stop_pump(self) -> None:
        self._pump_stop.set()
        task, self._pump_task = self._pump_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _dispatch(self, evt: dict) -> None:
        etype = evt.get("type", "")
        props = evt.get("properties") or {}

        if etype == "message.part.updated":
            part = props.get("part")
            if not isinstance(part, dict):
                return
            ptype = part.get("type")
            if ptype == "text":
                mid = part.get("messageID")
                # part 是**累积全文快照**，直接覆盖即可
                if mid and part.get("text"):
                    self._texts[mid] = part["text"]
            elif ptype == "tool":
                self._tool_part(part)
            return

        if etype == "message.updated":
            info = props.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                return
            completed = (info.get("time") or {}).get("completed")
            mid = info.get("id")
            # time.completed 只在消息完成时出现——完成前发文本会发成半截
            if mid and completed and mid not in self._announced:
                self._announced.add(mid)
                text = (self._texts.get(mid) or "").strip()
                if text:
                    self._emit(
                        EventType.ASSISTANT_TEXT,
                        {"text": text, "parent_tool_use_id": None},
                    )
            return

        if etype in ("permission.asked", "permission.v2.asked"):
            await self._reject_permission(props)

    def _tool_part(self, part: dict) -> None:
        """工具 part 按状态机发事件：pending/running → TOOL_CALL，终态 → TOOL_RESULT。

        一个 callID 的 TOOL_CALL 只发一次（running 时入参才齐全）；
        若快进到终态都没见过 running（极快的工具），补发一条再发结果，
        否则前端会收到一条没有来源的 TOOL_RESULT。
        """
        state = part.get("state") or {}
        status = state.get("status")
        call = part.get("callID")
        if not call:
            return

        if status in ("pending", "running") and call not in self._tools_announced:
            self._tools_announced.add(call)
            self._emit(
                EventType.TOOL_CALL,
                {
                    "id": call,
                    "name": part.get("tool"),
                    "input": state.get("input") or {},
                    "parent_tool_use_id": None,
                },
            )
            return

        if status == "completed":
            self._ensure_call(part)
            self._emit(
                EventType.TOOL_RESULT,
                {
                    "tool_use_id": call,
                    "content": _clip(_as_text(state.get("output"))),
                    "is_error": False,
                    "parent_tool_use_id": None,
                },
            )
        elif status == "error":
            self._ensure_call(part)
            self._emit(
                EventType.TOOL_RESULT,
                {
                    "tool_use_id": call,
                    "content": _clip(_as_text(state.get("error") or state.get("output"))),
                    "is_error": True,
                    "parent_tool_use_id": None,
                },
            )

    def _ensure_call(self, part: dict) -> None:
        call = part.get("callID")
        if not call or call in self._tools_announced:
            return
        self._tools_announced.add(call)
        state = part.get("state") or {}
        self._emit(
            EventType.TOOL_CALL,
            {
                "id": call,
                "name": part.get("tool"),
                "input": state.get("input") or {},
                "parent_tool_use_id": None,
            },
        )

    # ------------------------------------------------------------------
    # 权限兜底
    # ------------------------------------------------------------------

    async def _reject_permission(self, props: dict) -> None:
        """有权限询问 = 规则集漏了这个点。立刻拒绝让它显形，绝不干等。

        不拒绝的话 agent 会一直挂起等人，而 send() 在等这一轮结束——
        死锁。拒绝之后 agent 会收到拒绝并继续（或结束本轮），任务至少能走完，
        事件流里也留下了"这里缺一条规则"的线索。
        """
        request_id = props.get("id")
        assert self._http is not None and self._base is not None
        if request_id:
            directory = urllib.parse.quote(str(self.cwd), safe="")
            try:
                async with self._http.post(
                    f"{self._base}/permission/{request_id}/reply?directory={directory}",
                    json={"reply": "reject"},
                ) as resp:
                    await resp.read()
            except aiohttp.ClientError:
                pass

        tool_name = props.get("permission")
        tool_input = props.get("metadata")
        reason = (
            "OpenCode 出现了规则集没覆盖的权限询问，网关已自动拒绝——"
            "把这个权限点加进 COMMON_RULES 才能放行"
        )
        self._emit(
            EventType.PERMISSION_REQUEST,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "decision": "deny",
                "source": "not_allowed",
                "reason": reason,
            },
        )
        self._emit(
            EventType.PERMISSION_DECIDED,
            {
                "permission_id": request_id,
                "decision": "deny",
                "auto": True,
                "reason": reason,
            },
        )

    # ------------------------------------------------------------------
    # 终态
    # ------------------------------------------------------------------

    async def _flush_texts(self, reply: dict) -> None:
        """把 SSE 还没来得及宣布的助手文本补发掉。

        POST 的响应返回时，最后一条 message.updated 可能还在网络上。
        从 _texts 里把没宣布过的补上；终态消息本体若两处都没有
        （SSE 完全没跟上），直接从响应的 parts 里提取。
        """
        for mid in list(self._texts):
            if mid in self._announced:
                continue
            self._announced.add(mid)
            text = self._texts[mid].strip()
            if text:
                self._emit(
                    EventType.ASSISTANT_TEXT,
                    {"text": text, "parent_tool_use_id": None},
                )

        info = reply.get("info") if isinstance(reply, dict) else None
        mid = info.get("id") if isinstance(info, dict) else None
        if not mid or mid in self._announced:
            return
        self._announced.add(mid)
        text = "\n".join(
            part.get("text", "").strip()
            for part in (reply.get("parts") or [])
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
        if text:
            self._emit(
                EventType.ASSISTANT_TEXT,
                {"text": text, "parent_tool_use_id": None},
            )

    def _outcome(self, reply: dict) -> RunOutcome:
        info = reply.get("info") if isinstance(reply, dict) else None
        info = info if isinstance(info, dict) else {}
        time_ = info.get("time") or {}
        duration_ms: int | None = None
        if time_.get("created") and time_.get("completed"):
            duration_ms = int(time_["completed"]) - int(time_["created"])
        cost = info.get("cost")

        if self._interrupted:
            self._interrupted = False
            error = "本轮被主动中断"
            self._emit(EventType.TASK_FAILED, {"reason": "cancelled", "error": error})
            return RunOutcome(
                ok=False,
                reason="cancelled",
                error=error,
                cost_usd=cost,
                duration_ms=duration_ms,
                native_session_id=self.native_session_id,
            )

        # finish=="stop" 是正常结束；abort 是 None（上面已按中断处理），
        # 其余取值按失败算——宁可误报失败，不把没做完的事说成完成
        finish = info.get("finish")
        ok = finish == "stop"
        reason = finish or "no_finish"
        payload: dict = {"reason": reason, "cost_usd": cost, "duration_ms": duration_ms}
        if not ok:
            payload["error"] = (
                str(info.get("error"))
                if info.get("error")
                else f"OpenCode 一轮非正常结束（finish={finish!r}）"
            )
            self._emit(EventType.TASK_FAILED, payload)
            return RunOutcome(
                ok=False,
                reason=reason,
                error=payload["error"],
                cost_usd=cost,
                duration_ms=duration_ms,
                native_session_id=self.native_session_id,
            )

        self._emit(EventType.TASK_COMPLETED, payload)
        return RunOutcome(
            ok=True,
            reason=reason,
            cost_usd=cost,
            duration_ms=duration_ms,
            native_session_id=self.native_session_id,
        )


def _as_text(value: object) -> str:
    """工具输出可能是 str，也可能是结构化数据——统一压成能展示的文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


__all__ = ["OpenCodeSession"]
