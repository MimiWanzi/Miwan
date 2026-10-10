"""Gateway 命令行入口。

    python -m gateway agents      列出已注册的 Agent 接入及其在本机的可用状态
    python -m gateway projects    扫描各 Agent 的项目存储，落库，再从库里读回来
    python -m gateway run         跑一个真实任务（走完整状态机，事件落库）
    python -m gateway serve       启动 HTTP / WebSocket 网关（手机从这里进来）
    python -m gateway tasks       列出历史任务
    python -m gateway replay      从库里按 seq 回放某个任务的事件
    python -m gateway info        打印配置与运行环境
    python -m gateway code        要一个新的配对码（手机丢了码/码过期时用）
    python -m gateway devices     列出配对过的设备
    python -m gateway revoke ID   吊销一台设备

``run`` 与 ``replay`` 配成一对，是为了验证第 21 节的断线恢复：
run 出来的事件必须在库里，且 replay 能按原顺序一模一样地放出来。
``serve`` 把这个能力变成了常驻服务，事件经 ``EventBus`` 推给 WebSocket——
和 ``run`` 打印用的是同一条链路。

第 7 层的三条命令里，``devices`` 和 ``revoke`` **直接读写库**，不需要服务在跑
（设备状态本来就落在库里，服务每次请求都去读库，所以在外面吊销立刻生效）。
``code`` 不行——配对码只活在 ``serve`` 进程的内存里，所以它必须去问那个进程。
这不是偷懒，是这个方案的前提：配对码不落库，重启即换。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .adapters import DEFAULT_AUTONOMY, PENDING_AGENTS, AutonomyPolicy, detect_all, get
from .adapters.base import AgentInfo
from .auth import CODE_TTL, AuthService
from .config import load_config
from .events import Event, EventBus, EventType
from .models import Project, Session, Task, TaskStatus
from .runner import RunnerError, TaskRunner
from .server import run_server
from .store import Store
from .sync import discover, persist

SESSION_PREVIEW = 5
PROMPT_PREVIEW = 52
#: 事件泵的轮询间隔。任务结束时靠它把队列里剩的事件收干净。
POLL_TICK = 0.2


# ----------------------------------------------------------------------
# 展示工具
# ----------------------------------------------------------------------


def _fmt_time(timestamp: float | None) -> str:
    if not timestamp:
        return "从未"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _shorten(text: str, limit: int) -> str:
    """压成一行。事件正文里换行很常见，不压的话一行摘要会散成好几行。"""
    flat = text.replace("\n", " ⏎ ")
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _json_short(value: object, limit: int = 160) -> str:
    return _shorten(json.dumps(value, ensure_ascii=False), limit)


def _print_agent(info: AgentInfo) -> None:
    mark = {"ready": "●", "unconfigured": "○", "missing": "✗"}.get(info.status.value, "?")
    print(f"  {mark} {info.id:<13} {info.name}")
    line = f"      状态    {info.status.value}"
    if info.detail:
        line += f"  （{info.detail}）"
    print(line)
    print(f"      版本    {info.version or '—'}")
    print(f"      可执行  {info.executable or '—'}")

    caps = info.capabilities
    enabled = [
        label
        for label, ok in (
            ("事件流", caps.streaming_events),
            ("会话续接", caps.resume),
            ("中断", caps.interrupt),
            ("权限拦截", caps.permission_gate),
            ("成本上报", caps.cost_reporting),
            ("历史发现", caps.history_discovery),
        )
        if ok
    ]
    print(f"      能力    {'、'.join(enabled) or '无'}")
    print()


def _print_project(project: Project, sessions: list[Session]) -> None:
    branch = f"  [{project.git_branch}]" if project.git_branch else ""
    print(f"  {project.name}{branch}")
    print(f"    path      {project.path}")
    print(f"    sessions  {len(sessions)}")
    for session in sessions[:SESSION_PREVIEW]:
        print(f"      · {_fmt_time(session.last_active_at)}  {session.title}")
    remaining = len(sessions) - SESSION_PREVIEW
    if remaining > 0:
        print(f"      · …还有 {remaining} 个会话")
    print()


def _describe(event: Event) -> str:
    """把归一化事件压成一行摘要。这是人眼检查事件流质量的地方。"""
    payload = event.payload
    kind = event.type

    if kind is EventType.TASK_STARTED:
        tools = payload.get("tools")
        count = len(tools) if isinstance(tools, list) else "?"
        # tools 数量是关键自检点：--tools 参数写错会静默得到空工具集，
        # Agent 就会一个命令都执行不了，而表面上一切正常。
        return (f"model={payload.get('model')}  工具数={count}  "
                f"cwd={payload.get('cwd')}")
    if kind is EventType.ASSISTANT_TEXT:
        return _shorten(str(payload.get("text", "")), 200)
    if kind is EventType.TOOL_CALL:
        return f"{payload.get('name')}  {_json_short(payload.get('input'))}"
    if kind is EventType.TOOL_RESULT:
        mark = "✗" if payload.get("is_error") else "✓"
        return f"{mark} {_shorten(str(payload.get('content', '')), 180)}"
    if kind is EventType.PERMISSION_REQUEST:
        return (f"{payload.get('decision')}  {payload.get('tool_name')}  "
                f"(来源={payload.get('source')})")
    if kind in (EventType.TASK_COMPLETED, EventType.TASK_FAILED):
        detail = {k: v for k, v in payload.items() if k != "error"}
        line = _json_short(detail)
        if payload.get("error"):
            line += f"\n            error: {str(payload['error'])[:300]}"
        return line
    return _json_short(payload)


def _print_event(event: Event, elapsed_base: float | None, show_seq: bool) -> None:
    indent = "    └ " if event.payload.get("parent_tool_use_id") else ""
    prefix = f"#{event.seq:<5}" if show_seq else ""
    if elapsed_base is None:
        stamp = datetime.fromtimestamp(event.ts).strftime("%H:%M:%S")
    else:
        stamp = f"{event.ts - elapsed_base:6.2f}s"
    print(f"  {prefix}{stamp}  {event.type.value:<18}{indent}{_describe(event)}")


# ----------------------------------------------------------------------
# agents
# ----------------------------------------------------------------------


def cmd_agents(args: argparse.Namespace) -> int:
    # 检测要读 config.toml（opencode 的启动命令在那里面），先加载
    load_config()
    print("已注册的 Agent 接入：")
    print()
    for info in detect_all():
        _print_agent(info)

    if PENDING_AGENTS:
        print(f"尚未接入：{'、'.join(PENDING_AGENTS)}")
        print("接入方式：新增 adapters/<agent>/ 子包实现 AgentAdapter，")
        print("          在 gateway/adapters/__init__.py 里 register()。核心层不用改。")
    return 0


# ----------------------------------------------------------------------
# projects
# ----------------------------------------------------------------------


def cmd_projects(args: argparse.Namespace) -> int:
    config = load_config()

    started = time.perf_counter()
    # 扫描这一步和 HTTP 的 POST /api/projects/refresh 走的是同一个实现，
    # 免得两条路各自演化出不一样的屏蔽/合并规则
    adapters, scan = asyncio.run(discover(config.blocked_paths))
    elapsed = time.perf_counter() - started

    if not adapters:
        print("没有可用的 Agent 接入。先跑 `python -m gateway agents` 看检测结果。",
              file=sys.stderr)
        return 1

    print(f"已接入    {'、'.join(a.display_name for a in adapters)}")
    print(f"发现      {len(scan.projects)} 个项目 · {scan.session_total} 个会话"
          f"  （耗时 {elapsed:.2f}s）")
    if scan.skipped_blocked:
        print(f"已屏蔽    {len(scan.skipped_blocked)} 个："
              f"{'、'.join(scan.skipped_blocked)}")
    print()

    if args.json:
        print(json.dumps(
            {
                "projects": [p.model_dump() for p in scan.projects],
                "sessions": {
                    pid: [s.model_dump() for s in sessions]
                    for pid, sessions in scan.sessions.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    if args.no_sync:
        print("—— 仅扫描，未写库 ——")
        print()
        for project in scan.projects:
            _print_project(project, scan.sessions.get(project.id, []))
        return 0

    # 落库后从库里读回来——这一步同时验证了 store 的写入与查询
    store = Store(config.db_path)
    try:
        removed = persist(store, scan, prune=bool(adapters))
        projects = store.list_projects()
        print(f"已落库    {config.db_path}")
        print(f"读回      {len(projects)} 个项目"
              + (f"，清理掉 {removed} 个已消失的项目" if removed else ""))
        print()
        for project in projects:
            _print_project(project, store.list_sessions(project.id))
    finally:
        store.close()

    return 0


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------


def _resolve_project(store: Store, args: argparse.Namespace) -> Project:
    if args.project:
        project = store.get_project(args.project)
        if project is None:
            raise SystemExit(
                f"库里没有这个项目：{args.project}（先跑 `python -m gateway projects` 同步）"
            )
        return project

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    return store.ensure_project(str(cwd))


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config()

    adapter_cls = get(args.agent)
    if adapter_cls is None:
        print(f"未知 Agent：{args.agent}。用 `python -m gateway agents` 看有哪些。",
              file=sys.stderr)
        return 2

    info = adapter_cls.detect()
    if not info.ready:
        print(f"{info.name} 当前不可用：{info.detail or info.status.value}", file=sys.stderr)
        return 2

    autonomy = AutonomyPolicy(args.autonomy)
    store = Store(config.db_path)
    bus = EventBus()
    runner = TaskRunner(store, bus, default_agent=args.agent)

    try:
        project = _resolve_project(store, args)
    except SystemExit as exc:
        store.close()
        print(exc, file=sys.stderr)
        return 2

    print(f"Agent     {info.name} {info.version or ''}")
    print(f"项目      {project.name}  {project.path}")
    print(f"自主档位  {autonomy.value}")
    print(f"提示词    {args.prompt}")
    if args.resume_from:
        print(f"续接自    {args.resume_from}")
    print()
    print("注意：这会真实调用 API 并消耗额度。")
    print("─" * 74)

    started = time.time()

    async def go() -> Task | None:
        # 订阅总线来打印事件，而不是直接挂到 Adapter 的 emit 上——
        # 走的是和第 4 层 WebSocket 完全相同的那条路。这样这一层就顺带验证了
        # 事件从 Adapter 出来、经 store 拿到 seq、再广播出去整条链路。
        queue = bus.subscribe()

        try:
            task = runner.submit(
                project_id=project.id,
                prompt=args.prompt,
                agent_id=args.agent,
                autonomy=autonomy,
                resume_from=args.resume_from,
            )
        except RunnerError as exc:
            print(f"提交失败：{exc}", file=sys.stderr)
            return None

        print(f"任务 ID   {task.id}")
        print("─" * 74)

        waiter = asyncio.create_task(runner.wait(task.id))
        while True:
            # 队列排空且任务结束，才算真的收完——事件是先落库再广播的，
            # 所以任务结束时可能还有几条在路上。
            if waiter.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=POLL_TICK)
            except (TimeoutError, asyncio.TimeoutError):
                continue
            _print_event(event, started, show_seq=False)

        await runner.shutdown()
        return await waiter

    try:
        task = asyncio.run(go())
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130

    if task is None:
        store.close()
        return 1

    # 先把要读的都读完再关库——store.close() 之后任何查询都会抛
    # "Cannot operate on a closed database"
    final = store.get_task(task.id)
    events = store.event_count(task.id) if final else 0
    store.close()

    print("─" * 74)
    if final is None:
        print("读不回任务记录，数据库可能有问题。")
        return 1

    print(f"状态      {final.status.value}  reason={final.exit_reason or '—'}")
    if final.error:
        print(f"错误      {_shorten(str(final.error), 500)}")
    if final.cost_usd is not None:
        print(f"费用      ${final.cost_usd:.4f}")
    print(f"会话 ID   {final.native_session_id}")
    print(f"事件数    {events}  （`python -m gateway replay {final.id}` 可回放）")
    print(f"总耗时    {time.time() - started:.1f}s")

    return 0 if final.status is TaskStatus.COMPLETED else 1


# ----------------------------------------------------------------------
# tasks / replay
# ----------------------------------------------------------------------


def cmd_tasks(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(config.db_path)
    try:
        tasks = store.list_tasks(args.project)
        if not tasks:
            print("还没有任何任务。用 `python -m gateway run` 跑一个。")
            return 0

        print(f"共 {len(tasks)} 个任务" + (f"（项目 {args.project}）" if args.project else ""))
        print()
        for task in tasks:
            print(f"  {task.id}  {task.status.value:<12} {_fmt_time(task.created_at)}"
                  f"  {task.agent_id or '—'}")
            print(f"      {_shorten(task.prompt, PROMPT_PREVIEW)}")
            extra = []
            if task.cost_usd is not None:
                extra.append(f"${task.cost_usd:.4f}")
            extra.append(f"{store.event_count(task.id)} 事件")
            if task.exit_reason:
                extra.append(f"reason={task.exit_reason}")
            if task.native_session_id:
                extra.append(f"会话 {task.native_session_id[:8]}…")
            print(f"      {'  ·  '.join(extra)}")
            if task.error:
                print(f"      错误：{_shorten(str(task.error), 120)}")
            print()
    finally:
        store.close()
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    """把上次遗留的非终态任务标成 disconnected。

    ``serve`` 启动时会自动跑这一步；这里单独暴露一个入口是为了能手动触发它，
    以及在不想起服务的时候单独看它做了什么。
    """
    config = load_config()
    store = Store(config.db_path)
    bus = EventBus()
    runner = TaskRunner(store, bus)
    try:
        orphans = runner.recover()
        if not orphans:
            print("没有遗留的非终态任务。")
            return 0
        print(f"处理了 {len(orphans)} 个遗留任务，已标记为 disconnected：")
        print()
        for task in orphans:
            print(f"  {task.id}  {task.status.value} → disconnected")
            print(f"      {_shorten(task.prompt, PROMPT_PREVIEW)}")
            if task.native_session_id:
                print(f"      可从会话续接：--resume-from {task.native_session_id}")
            print()
    finally:
        store.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """起 HTTP / WebSocket 服务。阻塞到 Ctrl-C。"""
    config = load_config()
    if args.host or args.port:
        config = replace(
            config,
            host=args.host or config.host,
            port=args.port or config.port,
        )
    return run_server(config, open_browser=args.open)


def cmd_replay(args: argparse.Namespace) -> int:
    """从库里按 seq 顺序回放事件。

    这是第 21 节断线恢复的**验证手段**：run 出来的每一条事件都必须在库里，
    且顺序与实时流一致。第 8 层做重连补发时，补发走的就是同一个查询。
    """
    config = load_config()
    store = Store(config.db_path)
    try:
        task = store.get_task(args.task_id)
        if task is None:
            print(f"库里没有这个任务：{args.task_id}", file=sys.stderr)
            return 2

        events = store.events_since(args.task_id, args.since, limit=args.limit)
        print(f"任务      {task.id}   {task.status.value}")
        print(f"提示词    {_shorten(task.prompt, PROMPT_PREVIEW)}")
        print(f"项目      {task.project_id}")
        if task.native_session_id:
            print(f"会话 ID   {task.native_session_id}")
        print(f"回放      seq > {args.since} 的 {len(events)} 条事件"
              f"（库里共 {store.event_count(task.id)} 条）")
        print("─" * 74)
        for event in events:
            _print_event(event, elapsed_base=None, show_seq=True)
        print("─" * 74)
        if events:
            print(f"seq 范围   {events[0].seq} … {events[-1].seq}")
    finally:
        store.close()
    return 0


# ----------------------------------------------------------------------
# 设备（第 7 层）
# ----------------------------------------------------------------------


def _split_code(code: str) -> str:
    """``123456`` → ``123 456``。念出来、抄下来都不容易错。"""
    return f"{code[:3]} {code[3:]}"


def cmd_url(args: argparse.Namespace) -> int:
    """打出（或顺手打开）本机的主机登录链接。

    浏览器里"主机"身份只认主机 token，而配对码换不来它——没有这条路，
    凭证一丢（换浏览器、清缓存、30 天过期）主机功能就再也回不来了。

    打印到**网关这台机器自己的屏幕**上，跟配对码是同一条原则：
    能看到这条命令输出的人已经坐在这台机器前了。
    """
    config = load_config()
    host = args.host or config.host
    port = args.port or config.port
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{host}:{port}/?token={config.token}"
    print("在网关这台电脑的浏览器里打开下面这条链接，浏览器就拿到主机身份：")
    print()
    print(f"    {url}")
    print()
    print("链接里带着主机凭证：只在这台机器的屏幕上用，别复制给任何人。")
    if args.open:
        webbrowser.open(url)
        print("已尝试用默认浏览器打开。")
    return 0


def cmd_code(args: argparse.Namespace) -> int:
    """换一个新的配对码。

    必须问**正在跑的那个进程**：配对码只活在 ``serve`` 的内存里，
    这个命令自己生成一个的话，那个码跟服务器的状态毫无关系——
    用户会拿着一个永远配不上的码反复失败，而且失败信息（"配对码不对"）
    会把人引去怀疑自己抄错了。

    所以拿不到就跑不动，直接说不清楚。宁可说"服务没跑"，也不能给个假的。
    """
    config = load_config()
    # 和 `serve` 用同一套 --host/--port：不然用 --port 8799 起来的网关，
    # 这条命令会去敲 8765，报"连不上"，而服务明明在跑。
    host = args.host or config.host
    port = args.port or config.port
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{host}:{port}/api/pairing-code"

    request = urllib.request.Request(
        url, method="POST", data=b"", headers={"X-Token": config.token}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except Exception:  # noqa: BLE001 —— 报错信息读不出来不该盖住原始错误
            pass
        print(f"网关拒绝了这次请求（HTTP {exc.code}）。{detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"连不上网关 {url}：{exc}", file=sys.stderr)
        print("配对码只活在 serve 进程里，先起 `python -m gateway serve`。", file=sys.stderr)
        return 1

    code = payload.get("code")
    if not code:
        print(f"网关没有返回配对码：{payload.get('detail') or payload}", file=sys.stderr)
        print("（配对码只发给从本机发起的请求。远程调用只能换码，看不到码。）", file=sys.stderr)
        return 1

    print(f"新配对码    {_split_code(code)}")
    print(f"有效期      {int(CODE_TTL // 60)} 分钟，只能配一台设备")
    print()
    print("在手机上打开网关地址，输入这个码。")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    """列出配对过的设备。

    **不需要服务在跑**：设备状态在库里，服务每次请求都去读库，
    所以这个命令看到的就是服务器看到的那份。写成"必须起服务"没有好处，
    只会让人在排障时被迫先把服务拉起来。
    """
    config = load_config()
    store = Store(config.db_path)
    try:
        # 这里只用到设备的读写，用不到配对码；host_token 传空串即可——
        # authenticate() 这时永远不会被调到，而这个命令本来就没有请求要认。
        auth = AuthService(store, host_token=config.token)
        devices = auth.list_devices()
        if not devices:
            print("还没有配对过任何设备。")
            print()
            print(f"配对码在 `python -m gateway serve` 的启动横幅上，也能用 "
                  f"`python -m gateway code` 换一个。")
            return 0

        active = sum(1 for d in devices if not d.revoked)
        print(f"共 {len(devices)} 台设备（有效 {active}，已吊销 {len(devices) - active}）")
        print()
        for device in devices:
            mark = "✗" if device.revoked else "●"
            state = "已吊销" if device.revoked else "有效"
            print(f"  {mark} {device.id}  {device.name}")
            print(f"      状态      {state}")
            print(f"      配对时间  {_fmt_time(device.created_at)}")
            print(f"      最近使用  {_fmt_time(device.last_seen_at)}")
            print()
        print("吊销一台： python -m gateway revoke <设备 ID>")
    finally:
        store.close()
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    """吊销一台设备。同样不需要服务在跑——吊销是写库，服务下次请求就看不到它了。"""
    config = load_config()
    store = Store(config.db_path)
    try:
        auth = AuthService(store, host_token=config.token)
        target = next((d for d in auth.list_devices() if d.id == args.device_id), None)
        if target is None:
            print(f"库里没有这台设备：{args.device_id}", file=sys.stderr)
            print("用 `python -m gateway devices` 看有哪些。", file=sys.stderr)
            return 2
        if target.revoked:
            print(f"这台设备已经是吊销状态了：{target.name}")
            return 0

        # 真的吊销了才打印"已吊销"。库里可能在这两步之间变过（另一个进程
        # 抢先吊销了），那时 revoke() 返回 False——报成功会是彻头彻尾的谎话。
        if not auth.revoke(target.id):
            print("吊销没有生效（可能已被别的进程吊销）。用 devices 确认一下。",
                  file=sys.stderr)
            return 1

        print(f"已吊销    {target.id}  {target.name}")
        print()
        print("那台设备上的页面会立刻开始收 401，需要重新配对才能用。")
    finally:
        store.close()
    return 0


# ----------------------------------------------------------------------
# info
# ----------------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    config = load_config()
    print(f"数据目录      {config.data_dir}")
    print(f"数据库        {config.db_path}")
    print(f"静态页面      {config.web_dir}")
    print(f"监听地址      {config.host}:{config.port}")
    print(f"token         {config.token[:8]}…（共 {len(config.token)} 字符，"
          f"存于 {config.token_file}）")
    print(f"屏蔽清单      {len(config.blocked_paths)} 项"
          f"（自定义清单：{config.blocked_file}）")
    for blocked in config.blocked_paths:
        print(f"                {blocked}")

    store = Store(config.db_path)
    try:
        devices = AuthService(store, host_token=config.token).list_devices()
    finally:
        store.close()
    active = sum(1 for d in devices if not d.revoked)
    print(f"已配对设备    {len(devices)} 台（有效 {active}）"
          f"  `python -m gateway devices` 看详情")
    print()
    print("Agent 接入：")
    for info in detect_all():
        mark = {"ready": "●", "unconfigured": "○", "missing": "✗"}.get(
            info.status.value, "?"
        )
        print(f"  {mark} {info.id:<13} {info.status.value:<13} "
              f"{info.version or '—'}")
    return 0


# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gateway",
        description="Remote Agent Workspace · Local Agent Gateway",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_agents = sub.add_parser("agents", help="列出已注册的 Agent 接入与检测状态")
    p_agents.set_defaults(func=cmd_agents)

    p_projects = sub.add_parser("projects", help="发现项目与会话")
    p_projects.add_argument(
        "--no-sync", action="store_true", help="只扫描，不写入数据库"
    )
    p_projects.add_argument(
        "--json", action="store_true", help="以 JSON 输出（不写库）"
    )
    p_projects.set_defaults(func=cmd_projects)

    p_run = sub.add_parser("run", help="跑一个真实任务（落库，事件可回放）")
    p_run.add_argument("--prompt", required=True, help="交给 Agent 的提示词")
    p_run.add_argument("--agent", default="claude_code", help="用哪个 Agent 接入")
    p_run.add_argument("--project", help="项目 ID（`projects` 命令里能看到）")
    p_run.add_argument("--cwd", help="直接指定工作目录，默认当前目录")
    p_run.add_argument(
        "--autonomy",
        default=DEFAULT_AUTONOMY.value,
        choices=[p.value for p in AutonomyPolicy],
        help="自主档位：full 全部放行（只受绊线约束）、edit 可改文件、read_only 只读",
    )
    p_run.add_argument(
        "--resume-from", help="续接一个已有的 Agent 会话（native session id）"
    )
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="启动 HTTP / WebSocket 网关")
    p_serve.add_argument("--host", help="监听地址，默认 0.0.0.0（局域网可达）")
    p_serve.add_argument("--port", type=int, help="监听端口，默认 8765（REMOTE_AGENT_PORT 可覆盖）")
    p_serve.add_argument("--open", action="store_true",
                         help="启动后用本机浏览器打开控制台（带主机登录链接，exe 双击场景用）")
    p_serve.set_defaults(func=cmd_serve)

    p_tasks = sub.add_parser("tasks", help="列出历史任务")
    p_tasks.add_argument("--project", help="只列某个项目的")
    p_tasks.set_defaults(func=cmd_tasks)

    p_recover = sub.add_parser("recover", help="把上次遗留的非终态任务标为 disconnected")
    p_recover.set_defaults(func=cmd_recover)

    p_replay = sub.add_parser("replay", help="从库里按 seq 回放某个任务的事件")
    p_replay.add_argument("task_id", help="任务 ID")
    p_replay.add_argument("--since", type=int, default=0, help="只回放 seq 大于它的")
    p_replay.add_argument("--limit", type=int, default=1000)
    p_replay.set_defaults(func=cmd_replay)

    p_info = sub.add_parser("info", help="打印配置与运行环境")
    p_info.set_defaults(func=cmd_info)

    p_code = sub.add_parser("code", help="换一个新配对码（需要网关在跑）")
    p_code.add_argument("--host", help="网关监听地址，默认取配置")
    p_code.add_argument("--port", type=int, help="网关监听端口，默认取配置")
    p_code.set_defaults(func=cmd_code)

    p_url = sub.add_parser("url", help="打出本机的主机登录链接（--open 直接打开浏览器）")
    p_url.add_argument("--open", action="store_true", help="用默认浏览器打开链接")
    p_url.add_argument("--host", help="网关监听地址，默认取配置")
    p_url.add_argument("--port", type=int, help="网关监听端口，默认取配置")
    p_url.set_defaults(func=cmd_url)

    p_devices = sub.add_parser("devices", help="列出配对过的设备")
    p_devices.set_defaults(func=cmd_devices)

    p_revoke = sub.add_parser("revoke", help="吊销一台设备")
    p_revoke.add_argument("device_id", help="设备 ID（`devices` 命令里能看到）")
    p_revoke.set_defaults(func=cmd_revoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台常见 GBK 编码打不出 ●/✗ 这类状态标记（v5 起 missing
    # 状态真会出现，serve 横幅和 agents 都会打到它），UnicodeEncodeError
    # 会让整个命令/双击启动直接崩——退化成 ? 即可，中文不受影响。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # 非 TextIO / 已被重定向的极端情况
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
