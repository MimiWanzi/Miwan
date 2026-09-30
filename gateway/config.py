"""Gateway 配置。

所有可配置项集中在这里——其他模块不直接读环境变量，也不硬编码路径。

**刻意不含任何具体 Agent 的位置信息。** 第一版把 ``CLAUDE_EXE_CANDIDATES``
和 ``CLAUDE_PROJECTS_DIR`` 放在这里，结果是核心配置里躺着 Claude 的安装路径——
那正是"扩不出去"的开端：配置层一旦认识 Claude，换机器就要改核心代码。
现在这些是 ``adapters/claude_code/locate.py`` 的事。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    token: str
    data_dir: Path
    web_dir: Path
    blocked_paths: tuple[Path, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gateway.db"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "token.txt"

    @property
    def blocked_file(self) -> Path:
        return self.data_dir / "blocked_projects.txt"


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
        ROOT / "data",
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


def load_config() -> Config:
    data_dir = Path(os.environ.get("REMOTE_AGENT_DATA_DIR") or (ROOT / "data"))
    return Config(
        host=os.environ.get("REMOTE_AGENT_HOST", "0.0.0.0"),
        port=int(os.environ.get("REMOTE_AGENT_PORT", "8765")),
        token=load_token(data_dir),
        data_dir=data_dir,
        web_dir=ROOT / "web",
        blocked_paths=load_blocked_paths(data_dir),
    )
