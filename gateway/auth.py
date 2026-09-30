"""配对与设备凭证（第 7 层）。

## 为什么不是"一个 token 走天下"

这个网关能在用户电脑上以用户的身份执行任意命令。**谁拿到凭证，谁就有这台
机器的完整控制权。**所以凭证怎么送到手机，是这一层唯一真正重要的问题。

原来的做法是打印一条 `http://192.168.x.x:8765/?token=…` 让人复制到手机上。
问题不在于 token 不够长（32 字节，够），而在于**它的传播路径**：
用户会把它粘进聊天软件发给自己——那一刻它已经在别人的服务器上了，
还进了浏览器历史、剪贴板同步、可能的截图。

所以这一层把"长期凭证"和"一次性的递送手段"拆开：

    网关屏幕显示 6 位配对码  →  手机输入  →  换一份自己的长期凭证

配对码是一次性的、15 分钟过期的、有尝试次数上限的；长期凭证则**从不经过
聊天软件**——它在网关这边生成、由配对响应直接种进手机的 cookie，只存在于
那台手机和这个库里（且只存哈希）。用户手上要搬运的机密从"永久控制权"
缩小成一个 15 分钟后作废的一次性码。

## 两套凭证的关系

  - **设备凭证**：手机日常用的。可以单独吊销（"手机丢了"）。
  - **主机 token**（`data/token.txt`）：网关自己这台机器上的根凭证。
    在电脑前随时能用，也是重新配对的兜底。**它不再出现在任何网址里。**

主机 token 仍然被接受，是为了让"人就在电脑前"这个场景不需要任何仪式，
以及不让已有的命令行用法失效。

## 已知限制（写出来是因为不写就会被当成没有）

  - **明文 HTTP**：局域网内这些凭证是可以被嗅探的。用户 2026-09-30 决定
    暂不上 HTTPS，不是疏漏。完整清单见 `docs/security-notes.md`。
  - **配对码只有 20 bit 熵**：靠尝试次数限制和短有效期撑着，不是靠长度。
    所以限速**不是可选优化**，是这个方案成立的前提。
  - 限速状态在内存里，进程重启就清空。可接受：重启同时会换掉配对码。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .store import Store

if TYPE_CHECKING:  # 只为类型标注 import，运行时**不**依赖 aiohttp
    from aiohttp import web

__all__ = [
    "CODE_TTL",
    "MAX_ATTEMPTS",
    "ATTEMPT_WINDOW",
    "TOKEN_COOKIE",
    "TOKEN_MAX_AGE",
    "AuthService",
    "Device",
    "PairedDevice",
    "set_token_cookie",
]

#: 配对码有效期。15 分钟：够走到手机那边并输完，又不至于让一个念出来的
#: 码在咖啡店里长时间有效。
CODE_TTL = 15 * 60.0

#: 凭证 cookie 的名字与存活时间（30 天）。
#:
#: 放在这个模块而不是 ``server``：这是**凭证**的属性，不是传输的属性。
#: 更要紧的是「配对」和「日常请求」两条路都要种这个 cookie，
#: 各自定义一份的话，改一处漏一处，而症状是"配对成功了但下一秒就 401"。
TOKEN_COOKIE = "ra_token"
TOKEN_MAX_AGE = 30 * 24 * 3600


def set_token_cookie(response: "web.Response", token: str) -> None:
    """把凭证种进 cookie。

    ``httponly``：这个 cookie 前端一行 JS 都不该读到。手机页面只用它自动带着走，
    所以关掉脚本可见性不损失任何功能，却让"Agent 读到的一个恶意 HTML 进了
    事件流"这类问题够不着凭证。

    ``secure`` **不能开**：局域网是明文 HTTP，开了浏览器直接不接收这个 cookie，
    配对看起来成功、下一个请求立刻 401。
    """
    response.set_cookie(
        TOKEN_COOKIE,
        token,
        max_age=TOKEN_MAX_AGE,
        httponly=True,
        samesite="Lax",
        path="/",
    )

#: 同一个来源在窗口内允许的失败次数。**这个数字是方案的一部分，不是调优**——
#: 配对码 10^6 种、有效期内只允许错 5 次，撞中的概率是 5e-6。
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = CODE_TTL

#: 设备名长度上限，纯粹是别让手机传上来一坨东西进库。
MAX_NAME_LEN = 40


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    created_at: float
    last_seen_at: float | None = None
    revoked_at: float | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class PairedDevice:
    """配对成功的结果。``token`` **只在这里出现一次**，之后库里只有哈希。"""

    device: Device
    token: str


class PairingRejected(Exception):
    """配对失败。``reason`` 直接给用户看，所以要说人话。"""

    def __init__(self, reason: str, *, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def hash_token(token: str) -> str:
    """存哈希不存原文。

    这里用裸 sha256 而不是 PBKDF2/bcrypt 是**有意的**：那些算法的目的是
    拖慢对**低熵**口令的爆破，而设备凭证是 32 字节随机数（256 bit），
    根本没有爆破空间。为它做慢哈希只会让每个请求都多花几毫秒。

    （配对码是低熵的——但配对码从不落库，只活在内存里，所以也不受影响。）
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    """设备凭证 + 配对码。"""

    def __init__(self, store: Store, host_token: str) -> None:
        self._store = store
        self.host_token = host_token
        self._code: str | None = None
        self._code_issued_at = 0.0
        #: 每个来源的失败时间戳。用 deque 而不是计数，是为了让窗口**滑动**：
        #: 固定窗口里攻击者可以在窗口边界"穿越"，用掉两倍的尝试次数。
        self._failures: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------
    # 配对码
    # ------------------------------------------------------------------

    def issue_code(self, *, force: bool = False) -> str:
        """给出一个配对码，必要时换新的。

        过期的码会被换掉而不是复用——一个码的有效期必须真实，否则
        "15 分钟"这个数字就是假的，限速计算也跟着失真。
        """
        if force or self._code is None or self.code_expired():
            self._code = f"{secrets.randbelow(1_000_000):06d}"
            self._code_issued_at = time.time()
        return self._code

    def code_expired(self) -> bool:
        return (
            self._code is None
            or time.time() - self._code_issued_at > CODE_TTL
        )

    def code_seconds_left(self) -> float:
        if self._code is None:
            return 0.0
        return max(0.0, CODE_TTL - (time.time() - self._code_issued_at))

    def _failures_in_window(self, source: str, now: float) -> int:
        bucket = self._failures.get(source)
        if not bucket:
            return 0
        while bucket and now - bucket[0] > ATTEMPT_WINDOW:
            bucket.popleft()
        return len(bucket)

    def _record_failure(self, source: str, now: float) -> None:
        self._failures.setdefault(source, deque()).append(now)

    # ------------------------------------------------------------------
    # 配对
    # ------------------------------------------------------------------

    def pair(self, code: str, name: str, *, source: str = "?") -> PairedDevice:
        """用配对码换一份设备凭证。"""
        now = time.time()

        used = self._failures_in_window(source, now)
        if used >= MAX_ATTEMPTS:
            wait = ATTEMPT_WINDOW - (now - self._failures[source][0])
            raise PairingRejected(
                f"尝试次数太多，请等 {int(wait // 60) + 1} 分钟再试",
                retry_after=max(1.0, wait),
            )

        if self._code is None or self.code_expired():
            raise PairingRejected("配对码已过期，请在网关那边看一个新的")

        # 去掉**所有**空白，不只是首尾。界面上这个码是写成 ``123 456`` 的
        # （没人能一眼抄对六位连着的数字），用户、curl、将来的原生客户端
        # 都会原样把空格带过来。只 strip 首尾的话，"照着屏幕上抄"这个最自然
        # 的动作会稳定失败，而错误信息会把人引去怀疑自己看错了数字。
        cleaned = "".join(code.split())

        # 定长比较：配对码短，别让响应时间泄露"前几位对了"
        if not secrets.compare_digest(cleaned, self._code):
            self._record_failure(source, now)
            left = MAX_ATTEMPTS - self._failures_in_window(source, now)
            raise PairingRejected(
                f"配对码不对，还可以试 {left} 次" if left > 0 else "配对码不对，尝试次数已用完"
            )

        # 成功即作废：一个码只能换一台设备。
        # 不这么做的话，一个被人瞥见的码可以在有效期内被无限次使用。
        self._code = None
        self._failures.pop(source, None)

        token = secrets.token_urlsafe(32)
        device = Device(
            id=f"d-{secrets.token_hex(6)}",
            name=(name or "未命名设备").strip()[:MAX_NAME_LEN] or "未命名设备",
            created_at=now,
        )
        self._store.create_device(device.id, device.name, hash_token(token), now)
        return PairedDevice(device=device, token=token)

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def authenticate(self, presented: str | None) -> Device | None:
        """把一个出示的凭证换成设备。

        返回 None 表示**不是有效凭证**；返回的设备可能是已吊销的，
        调用方据此给出不同的提示——"被吊销了"和"不认识"对用户是两回事，
        前者意味着"你（或别人）主动停用了这台设备"。
        """
        if not presented:
            return None
        if secrets.compare_digest(presented, self.host_token):
            # 主机 token 不是一台设备，但它是有效凭证。
            # 用一个不在库里的合成身份代表它，这样调用方的分支不用特判。
            return Device(id="host", name="主机（本机 token）", created_at=0.0)

        row = self._store.device_by_token_hash(hash_token(presented))
        if row is None:
            return None
        return Device(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            revoked_at=row["revoked_at"],
        )

    def touch(self, device: Device) -> None:
        if device.id != "host":
            self._store.touch_device(device.id, time.time())

    # ------------------------------------------------------------------
    # 设备管理
    # ------------------------------------------------------------------

    def list_devices(self) -> list[Device]:
        return [
            Device(
                id=r["id"],
                name=r["name"],
                created_at=r["created_at"],
                last_seen_at=r["last_seen_at"],
                revoked_at=r["revoked_at"],
            )
            for r in self._store.list_devices()
        ]

    def revoke(self, device_id: str) -> bool:
        return self._store.revoke_device(device_id, time.time())
