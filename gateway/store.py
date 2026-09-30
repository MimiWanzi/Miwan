"""SQLite 持久化。

三条设计约束全部来自原型书第 21 节「断线恢复」，**不是**为将来上云额外加的成本：

  1. 任务状态落盘 —— 进程重启后能恢复
  2. 事件带单调递增 seq —— 重连时从 last_seq 补发
  3. 业务层不感知传输 —— 这个模块里不认识任何 WebSocket 概念

关于同步 SQLite：事件量级是每秒几条，本地写入在微秒级，直接同步调用足够。
真成为瓶颈时换 aiosqlite 即可，接口不变。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .events import Event, EventType
from .models import Project, Session, Task, TaskStatus, normalize_path, stable_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    path           TEXT NOT NULL UNIQUE,
    git_branch     TEXT,
    last_active_at REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id          TEXT,
    native_session_id TEXT,
    title             TEXT,
    git_branch        TEXT,
    created_at        REAL,
    last_active_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id),
    agent_id          TEXT,
    session_id        TEXT REFERENCES sessions(id),
    native_session_id TEXT,
    prompt            TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL,
    exit_reason       TEXT,
    error             TEXT,
    cost_usd          REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);

CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts      REAL NOT NULL,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);

CREATE TABLE IF NOT EXISTS permissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    ts          REAL NOT NULL,
    tool_name   TEXT,
    tool_input  TEXT,
    decision    TEXT,
    decided_at  REAL
);

-- 配对过的设备。第 7 层「按设备发凭证 + 单独吊销」。
--
-- 刻意存的是 token 的**哈希**而不是原文：这个库会被打开来看
-- （手机上要审计谁干了什么），原文一旦躺在这里，任何一次"把库拷出来看看"
-- 都等于把凭证一起拷走了。哈希的代价是多一次 sha256，几乎为零。
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   REAL NOT NULL,
    last_seen_at REAL,
    revoked_at   REAL
);
"""


#: 后加的列。SQLite 没有声明式的加列迁移，只能逐个 ALTER TABLE。
#:
#: 之所以不干脆删库重建：库里的 tasks / events 是**我们自己产生的真实数据**
#: （谁在什么时候让哪个 Agent 干了什么），不像 projects / sessions 那样
#: 随时能从 Agent 的存储里重扫出来。丢不起。
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "sessions": {"agent_id": "TEXT"},
    "tasks": {"agent_id": "TEXT", "native_session_id": "TEXT"},
}


class Store:
    """全部持久化操作的唯一入口。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """给已存在的表补上后加的列。

        ``CREATE TABLE IF NOT EXISTS`` 对已存在的表是**空操作**，所以往 SCHEMA 里
        加列不会作用到已有数据库上。这一点很容易漏，且漏了以后的表现很难查：
        新装的机器一切正常，而开发机上跑不起来，错误还是"no such column"。
        """
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            for name, decl in columns.items():
                if name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}"
                    )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Project / Session
    # ------------------------------------------------------------------

    def upsert_project(self, project: Project) -> None:
        self._conn.execute(
            """
            INSERT INTO projects (id, name, path, git_branch, last_active_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name           = excluded.name,
                path           = excluded.path,
                git_branch     = COALESCE(excluded.git_branch, projects.git_branch),
                last_active_at = MAX(
                    COALESCE(excluded.last_active_at, 0),
                    COALESCE(projects.last_active_at, 0)
                )
            """,
            (
                project.id,
                project.name,
                project.path,
                project.git_branch,
                project.last_active_at,
            ),
        )
        self._conn.commit()

    def upsert_session(self, session: Session) -> None:
        self._conn.execute(
            """
            INSERT INTO sessions
                (id, project_id, agent_id, native_session_id, title, git_branch,
                 created_at, last_active_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                agent_id          = COALESCE(excluded.agent_id, sessions.agent_id),
                native_session_id = excluded.native_session_id,
                title             = excluded.title,
                git_branch        = COALESCE(excluded.git_branch, sessions.git_branch),
                last_active_at    = COALESCE(excluded.last_active_at, sessions.last_active_at)
            """,
            (
                session.id,
                session.project_id,
                session.agent_id,
                session.native_session_id,
                session.title,
                session.git_branch,
                session.created_at,
                session.last_active_at,
            ),
        )
        self._conn.commit()

    def list_projects(self) -> list[Project]:
        rows = self._conn.execute(
            """
            SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id)
                   AS session_count
            FROM projects p
            ORDER BY COALESCE(p.last_active_at, 0) DESC, p.name
            """
        ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def get_project(self, project_id: str) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return self._row_to_project(row) if row else None

    def get_project_by_path(self, path: str) -> Project | None:
        """按路径查项目，用于"手动指定一个目录来跑任务"。

        用规范化后比较而不是 SQL 等值：Windows 路径大小写不敏感、分隔符可能
        正反混用，直接 ``WHERE path = ?`` 会漏掉本该匹配的那条。
        项目数量是几十的量级，全取回来在内存里比完全够用。
        """
        target = normalize_path(path)
        for row in self._conn.execute("SELECT * FROM projects"):
            if normalize_path(row["path"]) == target:
                return self._row_to_project(row)
        return None

    def ensure_project(self, path: str, name: str | None = None) -> Project:
        """拿到某个目录对应的项目，没有就建一个。

        这条路径是"手动添加项目路径"的兜底——不是所有 Agent 都能枚举历史
        （比如 OpenCode 要问它的 HTTP 服务），那种情况下用户手动指一个目录
        也必须能跑任务。
        """
        existing = self.get_project_by_path(path)
        if existing is not None:
            return existing

        project = Project(
            id=stable_id(path),
            name=name or (Path(path).name or path),
            path=path,
        )
        try:
            self.upsert_project(project)
        except sqlite3.IntegrityError:
            # path 上有 UNIQUE 约束：库里已有一条同路径但 id 不同的记录
            # （规范化差异导致 id 算得不一样）。以库里那条为准，不要硬插。
            self._conn.rollback()
            fallback = self.get_project_by_path(path)
            if fallback is None:
                raise
            return fallback
        return project

    def list_sessions(self, project_id: str) -> list[Session]:
        rows = self._conn.execute(
            """
            SELECT * FROM sessions WHERE project_id = ?
            ORDER BY COALESCE(last_active_at, 0) DESC
            """,
            (project_id,),
        ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def prune_missing_projects(self, keep_ids: set[str]) -> int:
        """删除本次扫描中已不存在的项目（会话随之级联删除）。

        项目与会话都是**派生数据**，随时能从 ~/.claude/projects 重新扫出来，
        所以删除是安全的。但有两点保护：

          - **带任务的项目不删**：任务是我们自己产生的真实数据，
            删掉项目会让它们变成悬空引用（外键已开启，删除本身也会失败）
          - 不删本次扫描跳过的项目（读不到 cwd、被屏蔽等），它们下次扫描可能又出现

        返回删除的项目数。
        """
        rows = self._conn.execute("SELECT id FROM projects").fetchall()
        removed = 0
        for row in rows:
            project_id = row["id"]
            if project_id in keep_ids:
                continue
            has_task = self._conn.execute(
                "SELECT 1 FROM tasks WHERE project_id = ? LIMIT 1", (project_id,)
            ).fetchone()
            if has_task:
                continue
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            removed += 1
        self._conn.commit()
        return removed

    def get_session_by_native_id(self, native_session_id: str) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE native_session_id = ?",
            (native_session_id,),
        ).fetchone()
        return self._row_to_session(row) if row else None

    # ------------------------------------------------------------------
    # Task
    # ------------------------------------------------------------------

    def create_task(self, task: Task) -> Task:
        self._conn.execute(
            """
            INSERT INTO tasks
                (id, project_id, agent_id, session_id, native_session_id, prompt,
                 status, created_at, started_at, finished_at, exit_reason, error, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.project_id,
                task.agent_id,
                task.session_id,
                task.native_session_id,
                task.prompt,
                task.status.value,
                task.created_at or time.time(),
                task.started_at,
                task.finished_at,
                task.exit_reason,
                task.error,
                task.cost_usd,
            ),
        )
        self._conn.commit()
        return task

    def update_task(self, task_id: str, **fields) -> None:
        """按关键字更新任务字段。status 传 TaskStatus 或字符串均可。"""
        if not fields:
            return
        if "status" in fields and isinstance(fields["status"], TaskStatus):
            fields["status"] = fields["status"].value
        columns = ", ".join(f"{key} = ?" for key in fields)
        self._conn.execute(
            f"UPDATE tasks SET {columns} WHERE id = ?",
            (*fields.values(), task_id),
        )
        self._conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, project_id: str | None = None) -> list[Task]:
        if project_id:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def active_tasks(self) -> list[Task]:
        """非终态任务——进程重启后用来判断哪些任务需要恢复。"""
        placeholders = ", ".join("?" for _ in _NON_TERMINAL)
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders})",
            _NON_TERMINAL,
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    # ------------------------------------------------------------------
    # Event
    # ------------------------------------------------------------------

    def append_event(self, event: Event) -> int:
        """落库并返回分配到的 seq。

        events.seq 是 INTEGER PRIMARY KEY AUTOINCREMENT，天然单调递增，
        补发/回放能力由此免费获得，不需要额外设计。
        """
        cursor = self._conn.execute(
            "INSERT INTO events (task_id, ts, type, payload) VALUES (?, ?, ?, ?)",
            (
                event.task_id,
                event.ts,
                event.type.value,
                json.dumps(event.payload, ensure_ascii=False),
            ),
        )
        self._conn.commit()
        seq = int(cursor.lastrowid or 0)
        event.seq = seq
        return seq

    def events_since(
        self, task_id: str, since_seq: int = 0, limit: int = 1000
    ) -> list[Event]:
        """取某个任务 seq 之后的事件——断线重连时补发用。"""
        rows = self._conn.execute(
            """
            SELECT * FROM events
            WHERE task_id = ? AND seq > ?
            ORDER BY seq
            LIMIT ?
            """,
            (task_id, since_seq, limit),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def event_count(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def has_terminal_event(self, task_id: str) -> bool:
        """这个任务的事件流里有没有一个终点。

        给 ``runner._finish`` 用：适配器已经在大多数路径上自己发过终态事件了，
        这里查一下才知道该不该补一条——补重了事件流会出现两个终点。
        """
        row = self._conn.execute(
            """
            SELECT 1 FROM events
            WHERE task_id = ? AND type IN (?, ?)
            LIMIT 1
            """,
            (task_id, EventType.TASK_COMPLETED.value, EventType.TASK_FAILED.value),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 设备（第 7 层）
    # ------------------------------------------------------------------

    def create_device(
        self, device_id: str, name: str, token_hash: str, created_at: float
    ) -> None:
        self._conn.execute(
            "INSERT INTO devices (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (device_id, name, token_hash, created_at),
        )
        self._conn.commit()

    def device_by_token_hash(self, token_hash: str) -> sqlite3.Row | None:
        """按哈希找设备。**吊销的也返回**——调用方要能区分"没这台设备"
        和"这台设备被吊销了"，后者该给出不一样的提示。"""
        return self._conn.execute(
            "SELECT * FROM devices WHERE token_hash = ?", (token_hash,)
        ).fetchone()

    def list_devices(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM devices ORDER BY created_at"
        ).fetchall()

    def revoke_device(self, device_id: str, ts: float) -> bool:
        cursor = self._conn.execute(
            "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (ts, device_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def touch_device(self, device_id: str, ts: float) -> None:
        """记一下最近使用时间。

        节流到分钟级：每个请求都写一次库没必要，而"这台设备上次什么时候
        用过"这个信息精确到分钟就足够回答它要回答的问题
        （"这台还是我的手机吗"）。
        """
        self._conn.execute(
            "UPDATE devices SET last_seen_at = ? WHERE id = ? AND "
            "(last_seen_at IS NULL OR last_seen_at < ?)",
            (ts, device_id, ts - 60.0),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 行 → 模型
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> Project:
        keys = row.keys()
        return Project(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            git_branch=row["git_branch"],
            last_active_at=row["last_active_at"],
            session_count=row["session_count"] if "session_count" in keys else 0,
        )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            project_id=row["project_id"],
            agent_id=row["agent_id"] or "",
            native_session_id=row["native_session_id"],
            title=row["title"] or "",
            git_branch=row["git_branch"],
            created_at=row["created_at"],
            last_active_at=row["last_active_at"],
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            agent_id=row["agent_id"] or "",
            session_id=row["session_id"],
            native_session_id=row["native_session_id"],
            prompt=row["prompt"],
            status=TaskStatus(row["status"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            exit_reason=row["exit_reason"],
            error=row["error"],
            cost_usd=row["cost_usd"],
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            seq=row["seq"],
            task_id=row["task_id"],
            type=EventType(row["type"]),
            ts=row["ts"],
            payload=json.loads(row["payload"]),
        )


_NON_TERMINAL = (
    TaskStatus.CREATED.value,
    TaskStatus.QUEUED.value,
    TaskStatus.RUNNING.value,
    TaskStatus.WAITING_APPROVAL.value,
)
