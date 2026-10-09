"""Gateway 配置。

所有可配置项集中在这里——其他模块不直接读环境变量，也不硬编码路径。

配置有四个来源，优先级从高到低（高的覆盖低的）：

  1. 命令行参数（``serve --host / --port``）
  2. 环境变量 ``REMOTE_AGENT_*``
  3. TOML 配置文件：``REMOTE_AGENT_CONFIG`` 指定的路径，或应用目录（Windows 是
     ``%LOCALAPPDATA%\\RemoteAgent``）下的 ``config.toml``
  4. 内置默认值——什么都没配也能起（自部署的第一次体验必须是"直接能跑"）

配置文件是给"迁移给别人用"的主入口：对方只改一个文件，不用翻代码。
所以有两条规定：

  - **显式指定的配置文件不存在就报错退出**，不静默回默认——
    "改了文件没生效"这类问题，一半是路径写错了，静默会让它变成玄学。
  - 文件里写了不认识的节/键，启动时点名警告。值拼错了静默用默认，
    和没写这个键一样查不着。

**刻意不含任何具体 Agent 的位置信息。** 第一版把 ``CLAUDE_EXE_CANDIDATES``
和 ``CLAUDE_PROJECTS_DIR`` 放在这里，结果是核心配置里躺着 Claude 的安装路径——
那正是"扩不出去"的开端：配置层一旦认识 Claude，换机器就要改核心代码。
现在这些是 ``adapters/claude_code/locate.py`` 的事。
"""

from __future__ import annotations

import os
import secrets
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .adapters.base import DEFAULT_AUTONOMY, AutonomyPolicy

ROOT = Path(__file__).resolve().parent.parent

#: 打包成 exe（PyInstaller）之后源码路径全部失真：**资源**（web/ 静态页）被解包到
#: ``sys._MEIPASS``（临时目录，进程结束就删）。
if getattr(sys, "frozen", False):
    ASSET_DIR = Path(getattr(sys, "_MEIPASS"))
else:
    ASSET_DIR = ROOT


def _app_dir() -> Path:
    """应用的固定「家」，配置和数据都住这里。

    必须与**从哪里启动、用 python 还是 exe 无关**——以前 python 启动用仓库根、
    exe 启动用 exe 旁边，两份数据库互不相认，换个启动方式"项目就没了"。
    """
    if env := os.environ.get("REMOTE_AGENT_HOME"):
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "RemoteAgent"
    return Path.home() / ".remote-agent"


APP_DIR = _app_dir()

#: 自动查找的配置文件名。
#:
#: 位置是**应用目录**（``_app_dir()``，Windows 上是 ``%LOCALAPPDATA%\RemoteAgent``），
#: **不是仓库根**——v2 把配置和数据统一搬进应用目录之后，放在仓库根的那份就不再
#: 被读了。这条注释以前写的是"仓库根"，是迁移时漏改的，误导过一轮排查。
#: 位置只有一个答案，由下面的 :data:`CONFIG_PATH` 给出，各处提示语都引用它。
CONFIG_FILENAME = "config.toml"

#: 配置文件的实际路径。横幅、检测失败的提示语都引用它——
#: "配置该放哪"在代码、启动输出、文档里必须是同一句话。
CONFIG_PATH = APP_DIR / CONFIG_FILENAME

#: 每个节里认识的键。之外的键不报错、但会点名警告——
#: 拼错一个键却静默落回默认值，是"改了配置没反应"的头号来源。
_KNOWN_KEYS: dict[str, set[str]] = {
    "server": {"host", "port", "public_url"},
    "storage": {"data_dir"},
    "agents": {"default"},
    "autonomy": {"default"},
    "auth": {"token"},
    "opencode": {"command"},
    "workbuddy": {"command"},
    "codex": {"command"},
    "qoder": {"command"},
    "notify": {"enabled", "channel", "key", "webhook_url"},
}


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    token: str
    data_dir: Path
    web_dir: Path
    default_agent: str
    default_autonomy: AutonomyPolicy
    #: 网关在外网可达的地址（如 Cloudflare Tunnel 的固定域名），没有隧道就留空。
    #: 唯一用途是监控台的「手机连接」二维码——扫码总比手输网址可靠。
    public_url: str = ""
    #: OpenCode 的启动命令（argv 前缀）。本机是源码仓库时没有 ``opencode`` 可执行文件，
    #: 得写成 bun + 入口脚本，例如
    #: ``command = ["bun", "run", "--cwd", "D:/…/packages/opencode", "src/index.ts"]``。
    #: 留空 = 从 PATH 上找 ``opencode``。
    opencode_command: tuple[str, ...] = ()
    #: WorkBuddy（内置 CodeBuddy CLI）的启动命令（argv 前缀）。留空 = 自动找：
    #: PATH 上的 codebuddy / cbc，或 WorkBuddy 安装目录里内置的那份
    #: （注册表卸载项反查，需要机器上有 Node）。
    workbuddy_command: tuple[str, ...] = ()
    #: Codex CLI 的启动命令（argv 前缀）。留空 = 自动找：PATH 上的 codex，
    #: 或 Codex 桌面应用内置的那份（%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\，
    #: hash 目录随版本变，取 mtime 最新）。
    codex_command: tuple[str, ...] = ()
    #: Qoder CLI 的启动命令（argv 前缀）。留空 = 自动找：PATH 上的
    #: qodercn / qoderclicn（npm 包 @qodercn-ai/qoderclicn）。
    qoder_command: tuple[str, ...] = ()
    #: 微信推送的初始值（运行时在设置页里改的存 ``data/notify.json``，优先于这里）。
    #: channel 取 gateway.notify.CHANNELS 之一；key 是 Server酱 SendKey / PushPlus
    #: token；webhook_url 是企业微信群机器人的完整地址。
    notify_enabled: bool = False
    notify_channel: str = "serverchan"
    notify_key: str = ""
    notify_webhook_url: str = ""
    blocked_paths: tuple[Path, ...] = ()
    #: 配置从哪个文件读来的；None = 没有配置文件，全默认。
    #: info 和启动横幅会显示它——"我改的 config.toml 生效了吗"看一眼就有答案。
    source: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gateway.db"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "token.txt"

    @property
    def blocked_file(self) -> Path:
        return self.data_dir / "blocked_projects.txt"


def _parse_command(section: str, raw: object) -> tuple[str, ...]:
    """``[xxx] command`` 的统一解析：字符串当单项，数组逐项转 str。"""
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        command = tuple(str(part) for part in raw if str(part).strip())
        if len(command) != len(raw):
            raise SystemExit(f"[{section}] command 里有空字符串项")
        return command
    raise SystemExit(f'[{section}] command 要写成字符串数组，如 ["{section}"]')


def default_blocked_paths() -> tuple[Path, ...]:
    """默认屏蔽的「项目」。

    这些不是真正的项目，而是 Agent 恰好被在那些目录里启动过：
      - 用户主目录：随手跑一次 `claude` 就会留下记录
      - 探针工作目录：我们自己造的
      - Gateway 自己的 data 目录

    只做**精确匹配**，不做前缀匹配——因为 ``C:\\Users\\lhy`` 是噪音，
    但它的子目录 ``C:\\Users\\lhy\\Desktop\\task_scheduler`` 是真实项目。
    前缀匹配会把后者一并误杀。
    """
    return (
        Path(os.path.expanduser("~")),
        ROOT / "probes" / "scratch",
        APP_DIR / "data",
    )


def load_blocked_paths(data_dir: Path) -> tuple[Path, ...]:
    """默认屏蔽清单 + 用户自定义清单。

    自定义清单放在 ``data/blocked_projects.txt``，一行一个路径，
    ``#`` 开头为注释。做成文件而不是代码里的配置，是为了让你不改代码就能屏蔽项目。
    """
    blocked = list(default_blocked_paths())
    extra_file = data_dir / "blocked_projects.txt"
    if extra_file.exists():
        for raw in extra_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            blocked.append(Path(line))
    return tuple(blocked)


def load_token(data_dir: Path) -> str:
    """读 token，没有就生成一个落盘。

    局域网场景下 token 是最小可用的认证手段。之所以第一天就要有它，
    是因为加认证和补认证的成本差一个量级——加只是多读一个字段，
    补则要回头改所有调用方。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    token_file = data_dir / "token.txt"
    if token_file.exists():
        existing = token_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    token_file.write_text(token, encoding="utf-8")
    return token


#: 最近一次 load_config 的结果。Adapter 在运行时要读机器相关配置
#: （如 opencode 的启动命令），但它们不在请求上下文里、拿不到 ctx——
#: 这里给一个只读的取回口，避免 Adapter 反向依赖调用方传参。
_LOADED: Config | None = None


def loaded_config() -> Config | None:
    return _LOADED


def _finalize(config: Config) -> Config:
    global _LOADED
    _LOADED = config
    return config


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"配置文件解析失败：{path}\n  {exc}")


def _warn_unknown_keys(raw: dict, path: Path) -> None:
    for section, body in raw.items():
        if not isinstance(body, dict):
            print(
                f"配置文件 {path}：顶层值 {section!r} 不在节里（要写成 [{section}]），已忽略",
                file=sys.stderr,
            )
            continue
        known = _KNOWN_KEYS.get(section)
        if known is None:
            print(f"配置文件 {path}：不认识的节 [{section}]，已忽略", file=sys.stderr)
            continue
        for key in body:
            if key not in known:
                print(
                    f"配置文件 {path}：[{section}] 里不认识的键 {key!r}，已忽略",
                    file=sys.stderr,
                )


def _as_int(value: object, what: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise SystemExit(f"{what} 必须是整数：{value!r}") from None


def _as_dir(value: object) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        # 相对路径锚在应用目录：从哪个目录启动网关，data 都落在同一个地方
        path = APP_DIR / path
    return path


def load_config(path: str | Path | None = None) -> Config:
    """装配配置。``path`` 给定时必须存在，否则报错退出。

    环境变量在文件之后生效（覆盖文件里的同名项）；命令行参数最后覆盖
    （那一步在 ``__main__`` 里用 ``dataclasses.replace`` 做）。
    """
    explicit: Path | None
    if path is not None:
        explicit = Path(path)
        if not explicit.is_file():
            raise SystemExit(f"指定的配置文件不存在：{explicit}")
    elif env_path := os.environ.get("REMOTE_AGENT_CONFIG"):
        explicit = Path(env_path)
        if not explicit.is_file():
            raise SystemExit(f"REMOTE_AGENT_CONFIG 指定的配置文件不存在：{explicit}")
    else:
        explicit = None

    if explicit is not None:
        source = explicit
        raw = _read_toml(explicit)
    elif (APP_DIR / CONFIG_FILENAME).is_file():
        source = APP_DIR / CONFIG_FILENAME
        raw = _read_toml(source)
    else:
        source = None
        raw = {}
    if source is not None:
        _warn_unknown_keys(raw, source)

    def section(name: str) -> dict:
        body = raw.get(name)
        return body if isinstance(body, dict) else {}

    server = section("server")
    storage = section("storage")
    agents = section("agents")
    autonomy = section("autonomy")
    auth = section("auth")
    opencode = section("opencode")
    workbuddy = section("workbuddy")
    codex = section("codex")
    qoder = section("qoder")
    notify = section("notify")

    host = str(server.get("host", "0.0.0.0"))
    port = _as_int(server.get("port", 8765), "[server] port")
    data_dir = _as_dir(storage.get("data_dir", "data"))

    env_host = os.environ.get("REMOTE_AGENT_HOST")
    if env_host:
        host = env_host
    env_port = os.environ.get("REMOTE_AGENT_PORT")
    if env_port:
        port = _as_int(env_port, "REMOTE_AGENT_PORT")
    env_data = os.environ.get("REMOTE_AGENT_DATA_DIR")
    if env_data:
        data_dir = _as_dir(env_data)

    public_url = str(server.get("public_url", "")).strip().rstrip("/")
    env_public = os.environ.get("REMOTE_AGENT_PUBLIC_URL", "").strip()
    if env_public:
        public_url = env_public.rstrip("/")

    default_agent = str(agents.get("default", "claude_code")).strip()
    if not default_agent:
        raise SystemExit("[agents] default 不能为空（填 claude_code，或先 `python -m gateway agents` 看有哪些）")

    raw_autonomy = str(autonomy.get("default", DEFAULT_AUTONOMY.value))
    try:
        default_autonomy = AutonomyPolicy(raw_autonomy)
    except ValueError:
        options = "、".join(p.value for p in AutonomyPolicy)
        raise SystemExit(f"[autonomy] default 只能是 {options}：{raw_autonomy!r}") from None

    opencode_command = _parse_command("opencode", opencode.get("command", []))
    workbuddy_command = _parse_command("workbuddy", workbuddy.get("command", []))
    codex_command = _parse_command("codex", codex.get("command", []))
    qoder_command = _parse_command("qoder", qoder.get("command", []))

    notify_channel = str(notify.get("channel", "serverchan")).strip() or "serverchan"

    env_token = os.environ.get("REMOTE_AGENT_TOKEN", "").strip()
    token_override = str(auth.get("token", "") or "").strip() or env_token
    if token_override:
        # 覆盖了自动生成的 token 也要把数据目录建出来——Store 落库假定它存在
        data_dir.mkdir(parents=True, exist_ok=True)
        token = token_override
    else:
        token = load_token(data_dir)

    return _finalize(Config(
        host=host,
        port=port,
        token=token,
        data_dir=data_dir,
        web_dir=ASSET_DIR / "web",
        default_agent=default_agent,
        default_autonomy=default_autonomy,
        public_url=public_url,
        opencode_command=opencode_command,
        workbuddy_command=workbuddy_command,
        codex_command=codex_command,
        qoder_command=qoder_command,
        notify_enabled=bool(notify.get("enabled", False)),
        notify_channel=notify_channel,
        notify_key=str(notify.get("key", "") or ""),
        notify_webhook_url=str(notify.get("webhook_url", "") or ""),
        blocked_paths=load_blocked_paths(data_dir),
        source=source,
    ))
