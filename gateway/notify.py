"""微信推送：任务完成 / 失败 / 等审批 / 撞绊线，四种「错过有代价」的事。

定位是**网页之外的事件触达**：手机不会一直停在任务页上，而这四件事
有一个共同点——错过就有代价（钱白花、活白干、任务卡在人身上、
AI 正在想干危险的事）。网页里本来就能看到的东西不推，推送不是通知中心。

三条工程约束：

  1. **fire-and-forget**。推送永远不阻塞任务、不阻塞事件循环；
     推送失败只打日志。用户不会为了没收到推送接受"任务因此变慢"。
  2. 渠道都是"一个 HTTP POST"，不做 SDK、不做重试队列。收不到就收不到，
     网页上的事件流永远是权威记录。
  3. 发送走**标准库 urllib + 线程池**，刻意不 import aiohttp——
     runner 会 import 本模块，而 runner 的分层承诺是"不碰 aiohttp"。

渠道（都是扫码/拉群五分钟内能搞定的那种）：

  - **Server酱**：微信服务号推送，sct.ftqq.com 扫码拿 SendKey，最省事，默认。
  - **PushPlus**：同类的公众号推送，填 token。
  - **企业微信群机器人**：建个群加个机器人，拿 webhook 地址。没有个人微信
    的打扰限制，适合当"任务日志群"。

设置两个来源：``config.toml`` 的 ``[notify]``（无人值守/迁移场景），
和设置页写下的 ``data/notify.json``（运行时改的，优先）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config
from .events import Event, EventType
from .models import Task

log = logging.getLogger("gateway.notify")

#: 渠道标识。前端下拉框、config.toml、notify.json 都用这组值。
CHANNELS = ("serverchan", "pushplus", "wecom_webhook")

CHANNEL_NAMES = {
    "serverchan": "Server酱（微信服务号）",
    "pushplus": "PushPlus（微信公众号）",
    "wecom_webhook": "企业微信群机器人",
}

_SETTINGS_FILENAME = "notify.json"

#: 单次推送的 HTTP 超时。推送是旁路，慢了就当失败。
_HTTP_TIMEOUT = 10.0

#: 正文/入参在推送里的截断长度。企业微信 markdown 上限 4096 字节，
#: 一条 UTF-8 中文三个字节，1500 字符怎么都够一条通知用了。
_BODY_LIMIT = 1500
_INPUT_LIMIT = 300


@dataclass
class NotifySettings:
    enabled: bool = False
    channel: str = "serverchan"
    #: Server酱的 SendKey / PushPlus 的 token
    key: str = ""
    #: 企业微信群机器人的 webhook 完整地址
    webhook_url: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def problem(self) -> str | None:
        """这份设置能不能真的发出去。返回原因，``None`` = 可以。"""
        if self.channel not in CHANNELS:
            return f"channel 只能是 {'、'.join(CHANNELS)}"
        if self.channel == "wecom_webhook":
            url = self.webhook_url.strip()
            # 本机回环的 http 放行：自建推送网关和测试都指到这里，拦它没有收益
            if not (
                url.startswith("https://")
                or url.startswith("http://127.0.0.1")
                or url.startswith("http://localhost")
            ):
                return "企业微信机器人要填完整的 webhook 地址（https://qyapi.weixin.qq.com/…）"
        elif not self.key.strip():
            return "这个渠道要填 key"
        return None


def load_settings(data_dir: Path, config: Config) -> NotifySettings:
    """设置页的 JSON 优先；没有就用 ``config.toml [notify]``（都没有 = 关着）。

    优先级这么定：json 是设置页写下的「用户最新的意图」，toml 是开机时的
    初始值。两者都存在时，按 toml 来会让设置页"保存了却不生效"。
    """
    path = data_dir / _SETTINGS_FILENAME
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            channel = str(raw.get("channel") or "serverchan")
            return NotifySettings(
                enabled=bool(raw.get("enabled")),
                channel=channel if channel in CHANNELS else "serverchan",
                key=str(raw.get("key") or ""),
                webhook_url=str(raw.get("webhook_url") or ""),
            )
    return NotifySettings(
        enabled=bool(config.notify_enabled),
        channel=config.notify_channel if config.notify_channel in CHANNELS else "serverchan",
        key=config.notify_key,
        webhook_url=config.notify_webhook_url,
    )


class Notifier:
    """把消息推到微信。调用 :meth:`notify` 即返回，发送在后台进行。"""

    def __init__(self, settings: NotifySettings, settings_path: Path) -> None:
        self.settings = settings
        self._settings_path = settings_path
        self._pending: set[asyncio.Task[None]] = set()

    @classmethod
    def from_config(cls, config: Config) -> Notifier:
        return cls(load_settings(config.data_dir, config), config.data_dir / _SETTINGS_FILENAME)

    def update_settings(self, settings: NotifySettings) -> None:
        """换设置并落盘。下次启动从 json 读回来。"""
        self.settings = settings
        self._settings_path.write_text(
            json.dumps(settings.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def notify(self, title: str, body: str) -> None:
        """fire-and-forget：调度后台发送，立刻返回。

        没有事件循环（CLI 同步上下文）时只能放弃——但所有调用点都在
        runner 的事件路径上，那里永远有循环。
        """
        if not self.settings.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("推送只能在事件循环里发（本次丢弃）：%s", title)
            return
        task = loop.create_task(self._send_logged(title, body))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send_logged(self, title: str, body: str) -> None:
        ok, detail = await self.send_once(title, body)
        if not ok:
            log.warning("推送没发出去（%s）：%s", detail, title)

    async def send_once(self, title: str, body: str) -> tuple[bool, str]:
        """发一条并报告结果——设置页的「测试推送」靠它给用户反馈。"""
        if not self.settings.enabled:
            return False, "推送没有开启"
        problem = self.settings.problem()
        if problem is not None:
            return False, problem

        s = self.settings
        if s.channel == "serverchan":
            ok, detail, payload = await asyncio.to_thread(
                _http_post,
                f"https://sctapi.ftqq.com/{s.key.strip()}.send",
                form={"title": title[:64], "desp": body},
            )
            good = payload.get("code") == 0
        elif s.channel == "pushplus":
            ok, detail, payload = await asyncio.to_thread(
                _http_post,
                "https://www.pushplus.plus/send",
                payload={"token": s.key.strip(), "title": title, "content": body, "template": "txt"},
            )
            good = payload.get("code") == 200
        else:
            content = f"**{title}**\n{body}"[:_BODY_LIMIT]
            ok, detail, payload = await asyncio.to_thread(
                _http_post,
                s.webhook_url.strip(),
                payload={"msgtype": "markdown", "markdown": {"content": content}},
            )
            good = payload.get("errcode") == 0

        if not ok:
            return False, detail
        if good:
            return True, "已送达"
        message = (
            payload.get("message") or payload.get("errmsg") or payload.get("msg") or "渠道返回不明确"
        )
        return False, str(message)[:120]


def _http_post(
    url: str, *, form: dict | None = None, payload: dict | None = None
) -> tuple[bool, str, dict]:
    """阻塞的 POST，跑在线程池里。返回（HTTP 是否 200，人话摘要，响应 JSON）。"""
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        body = urllib.parse.urlencode(form or {}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也带响应体，渠道的错误说明常在那里
        text = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except (OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}", {}
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    summary = f"HTTP {status}" if status == 200 else f"HTTP {status}：{text[:120]}"
    return status == 200, summary, parsed


# ----------------------------------------------------------------------
# 事件 → 消息
# ----------------------------------------------------------------------


def _brief(prompt: str, limit: int = 40) -> str:
    text = (prompt or "").strip()
    first = text.splitlines()[0] if text else ""
    if not first:
        return "（无标题任务）"
    return first[:limit] + ("…" if len(first) > limit else "")


def _duration(task: Task) -> str:
    if not task.started_at:
        return "不到 1 秒"
    secs = max(0, int((task.finished_at or time.time()) - task.started_at))
    if secs < 60:
        return f"{secs} 秒"
    return f"{secs // 60} 分 {secs % 60} 秒"


def _input_brief(tool_input: object) -> str:
    try:
        text = json.dumps(tool_input, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(tool_input)
    return text[:_INPUT_LIMIT] + ("…（已截断）" if len(text) > _INPUT_LIMIT else "")


def event_message(task: Task, event: Event) -> tuple[str, str] | None:
    """一条事件值不值得推、推什么。返回（标题，正文）；``None`` = 不推。

    推送的口吻是"管家汇报"：标题说发生了什么，正文给出要紧的数字和原因。
    用户自己刚按的取消不推——他刚操作完，再推一条是骚扰。
    """
    brief = _brief(task.prompt)
    if event.type is EventType.TASK_COMPLETED:
        parts = [f"耗时 {_duration(task)}"]
        cost = event.payload.get("cost_usd")
        if cost is not None:
            parts.append(f"花费 ${float(cost):.4f}")
        return f"✅ 任务完成：{brief}", "、".join(parts)

    if event.type is EventType.TASK_FAILED:
        if event.payload.get("reason") in ("gateway_restart", "cancelled"):
            return None
        error = str(event.payload.get("error") or "").strip()
        detail = f"耗时 {_duration(task)}"
        if error:
            detail += f"\n{error[:200]}"
        else:
            detail += "，没有留下错误信息"
        return f"❌ 任务失败：{brief}", detail

    if event.type is EventType.PERMISSION_REQUEST:
        tool = str(event.payload.get("tool_name") or "未知工具")
        what = _input_brief(event.payload.get("tool_input"))
        if event.payload.get("source") == "tripwire":
            return (
                f"⛔ 撞上绊线：{tool}",
                f"任务「{brief}」想执行的操作被安全规则拦下了：\n{what}",
            )
        return (
            f"🔑 等你批准：{tool}",
            f"任务「{brief}」想执行：\n{what}\n\n打开网页答复这条请求，任务在等它。",
        )

    return None
