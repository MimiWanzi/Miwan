"""权限通道：网关怎么回答"这个操作能不能做"。

**先把问题说清楚**，因为这里最容易想当然。

无人的 ``claude -p`` 模式下**不存在"照常工作"这个选项**。``--help`` 原文：

    --permission-prompts <target>
      "host" (the SDK host or --permission-prompt-tool)
      "none" (nobody: anything that would prompt is denied automatically)
      default: "host"

也就是说权限问题**必然**会被抛出来，只是"谁来答"。不配回答者就没人接；
配 ``none`` 则任何本该提示的操作被自动拒绝，Agent 干不了活。
所以问题从来不是"要不要加一层拦截"，而是"这道题我们怎么答"。

用户定的策略是**默认全放、极短绊线**：绝大多数操作直接放行，只有少数几条
"做了就收不回来"的操作才拦。

## 这一层在做哪一半

``Policy.decide()`` 是**纯函数**：给一个 (工具名, 工具参数)，返回
放行 / 拒绝 / 问一下。不碰网络、不碰数据库、不知道 Claude 的存在，
因此可以脱开 API 单独测——这一层最该被穷举测试，因为它错一次的代价是
删掉用户的东西。

**"问一下"这一半依赖运行时管道**（``--permission-prompt-tool`` 指向一个
MCP server，网关通过它接管回答），那是第 6 层并行探针要验证的。
无论管道最后走不走得通，这个模块都不用改：管道只决定"问"能不能送达手机，
决定不了"该不该问"。

## 绊线的能力边界（务必读）

绊线是**正则匹配命令行文本**，不是沙箱。它拦得住"模型顺手用了个危险命令"，
**拦不住**存心绕过的东西：

    iex "Remove" + "-Item -Recurse -Force ."      # 拼接
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("...")) | iex
    Remove-Item -Path (-join ('R','e','c','u','r','s','e')) ...

任何正则清单都拦不住这些。所以它的定位是**绊线**：碰到了就停下来说一声，
不是"安全边界"。真正的边界是 AutonomyPolicy（结构性限权）和将来可能的
容器/低权限账户。这一点写在代码里，因为一个被当成安全边界的正则清单
比没有清单更危险——它给人虚假的安心。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = [
    "DEFAULT_TRIPWIRES",
    "ASK_TIMEOUT_SECONDS",
    "Decision",
    "Policy",
    "Tripwire",
    "Verdict",
    "load_policy",
    "tripwires_file",
]


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    """推给手机，等人点。**目前到不了**——见下面 :data:`ASK_TIMEOUT_SECONDS`。"""


#: 问手机等多久。用户定的 2 分钟：太短则人还没掏手机就过期了，
#: 太长则这一轮一直悬着。
#:
#: **当前用不上**，写在这里是因为它定义了"问一下"应该是什么行为，
#: 而那个通道还没落地。实测（probes/out/permissions_run2.txt 组合 E）：
#: 声明式通道（``--disallowedTools``）拦下来就是**当场拒**，CLI 没有
#: "先挂起等人回答"这个状态。要做到 "ask"，得让 ``--permission-prompt-tool``
#: 指向一个 MCP server 由网关接管回答——CLI 收这个参数，但 ``--help`` 里没有，
#: 属于未验证依赖。用户 2026-09-30 决定接受现状、暂不做，因此：
#:
#:   **今天的行为是"绊线命中 → 当场拒绝 + 回合结束时通知手机"。**
#:
#: 通知晚是这套方案的已知代价：拒绝记录在 ``result.permission_denials`` 里，
#: 只有回合结束才拿得到。一个跑 10 分钟的任务，你在第 10 分钟才知道它撞了绊线。
ASK_TIMEOUT_SECONDS = 120.0

#: 命中绊线时的决定。
#:
#: 取 DENY 而不是 ASK，是因为**这个值是给今天用的**，不是给将来用的：
#: 现在没有任何通道能把"问一下"送达手机，写 ASK 会让读代码的人以为
#: 手机上会弹审批框。等 MCP 通道落地，这里改成 ASK 才算数。
#: 用户选的"等 2 分钟 → 拒绝"里，超时那一半就是 DENY。
TRIPWIRE_DECISION = Decision.DENY

#: 哪些工具的参数里有"命令行"。Windows 上是 PowerShell，别的平台是 Bash。
#: 写多个名字是因为这份清单要能跟着 Adapter 换平台，而不是绑死本机。
SHELL_TOOLS = frozenset({"PowerShell", "Bash", "shell", "pwsh", "Shell", "run_command"})


@dataclass(frozen=True)
class Tripwire:
    """一条绊线。

    ``scope`` 决定匹配哪段文本：``shell`` 只看命令行，``any`` 看所有字符串参数。
    区分开是有意义的——``Remove-Item`` 出现在命令里是绊线，出现在
    Write 的**文件内容**里只是文本（比如正在写这份说明），不该误伤。

    ``globs`` 是**同一条规则的另一种写法**，喂给 ``--disallowedTools``。
    为什么要写两遍：CLI 的正则是行不通的（它只吃 glob），而 glob 又表达不了
    ``scope``、跨参数匹配这些——所以两种都得有。它们放在同一个 dataclass 里
    不是巧合，是为了**改一条规则时另一条就在眼前**：分散在两个文件里的
    双份清单，迟早会有一处忘了改，而那种不一致只有出事时才发现。
    """

    id: str
    title: str
    pattern: re.Pattern[str]
    why: str = ""
    scope: str = "shell"
    globs: tuple[str, ...] = ()

    def matches(self, tool_name: str, text: str) -> bool:
        if self.scope == "shell" and tool_name not in SHELL_TOOLS:
            return False
        return bool(self.pattern.search(text))

    def disallowed_tools(self) -> list[str]:
        """展开成 ``--disallowedTools`` 的参数。

        glob 是**尽力而为的第二道**，不是权威：它拦得住
        ``Remove-Item -Recurse -Force``，拦不住拼接出来的等价命令。
        权威在 :meth:`Policy.decide`，因为它能表达 scope、能带上原因、
        而且将来能改判成"问一下"。这里只负责让 CLI 自己也别放行。
        """
        return [f"PowerShell({g})" for g in self.globs]
        # 只展开 PowerShell：本机（Windows）只有这个 shell 工具。
        # 换平台时 BASH_TOOLS 的展开要一起加——否则新平台上绊线会静默失效，
        # 而"静默失效的防线"比"没有防线"更糟。

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "why": self.why}


def _rx(pattern: str) -> re.Pattern[str]:
    # 命令行大小写不敏感（PowerShell 本身就不敏感）；不设 DOTALL——
    # 按行匹配能让 [^\n]{0,200} 这种有界通配保持线性，避免回溯爆炸
    return re.compile(pattern, re.IGNORECASE)


#: 默认绊线。用户选的是"极短"这一档——只有**做了就收不回来**的操作才在里面。
#: 判断标准是"能不能撤销"，不是"看起来危不危险"：改文件能撤销（有 git），
#: 删目录、格式化盘、改权限、强推覆盖远程历史不能。
#:
#: **顺序即优先级，先命中者胜。** 这条规则是有代价的，写清楚免得有人随手重排：
#: ``Remove-Item HKLM:\\... -Recurse`` 同时命中"递归删除"和"删注册表"，
#: 两条都对，但手机上只显示先命中的那条。所以**更具体的规则必须排在前面**——
#: 用户看到"删注册表"就知道该不该点，看到"递归删除"还得自己猜删的是哪儿。
DEFAULT_TRIPWIRES: tuple[Tripwire, ...] = (
    # 具体规则在前：注册表删除也是一种"递归删除"，但这条说得更清楚
    Tripwire(
        "registry-delete",
        "删注册表",
        _rx(r"\breg\s+delete\b|\bremove-item\b[^\n]{0,80}?\b(hklm|hkcu|hkey_)"),
        "注册表没有回收站",
        globs=("reg delete*", "Remove-Item*HKLM:*", "Remove-Item*HKCU:*"),
    ),
    Tripwire(
        "rm-recurse",
        "递归或强制删除",
        # 短横线参数要**逐个列全**，不能指望 `-force` 顺带覆盖 `-f`：
        # 第一版只写了 -recurse/-force//s/*，结果 `rm -rf` 大摇大摆地过去了——
        # 而它是最常见的那一个。测试用例当场逮住，这就是穷举比抽样值钱的地方。
        # -rf / -fr 要单列：中间没有词边界，`-r\b` 匹配不到 `-rf` 里的 r。
        _rx(r"\b(remove-item|rm|del|erase|rd|ri|rmdir)\b[^\n]{0,200}?"
            r"(-recurse\b|-force\b|-rf\b|-fr\b|-r\b|-f\b|--recursive\b|--force\b|/s\b|/q\b|\*)"),
        "删掉的目录进不了回收站，git 也救不了没被跟踪的文件",
        # 已实测生效：这组 glob 真的让 claude 拒绝了删除，且它没有绕路，
        # 拒绝记录落在 result.permission_denials 上（见 probes/out/permissions_run2.txt）
        globs=("Remove-Item*-Recurse*", "Remove-Item*-Force*", "rm -rf*", "rm -fr*",
               "ri -Recurse*", "rd /s*", "del /s*"),
    ),
    Tripwire(
        "disk-wipe",
        "磁盘/分区操作",
        _rx(r"\b(format-volume|clear-disk|initialize-disk|new-partition|remove-partition|diskpart)\b"),
        "这几个命令的对象是整个分区，不是项目里的文件",
        globs=("Format-Volume*", "Clear-Disk*", "Initialize-Disk*", "diskpart*"),
    ),
    Tripwire(
        "git-rewrite",
        "强推或改写历史",
        _rx(r"\bgit\b[^\n]{0,80}?\b(push\b[^\n]{0,40}?(-f\b|--force\b)|"
            r"reset\b[^\n]{0,20}?--hard\b|clean\b[^\n]{0,20}?-[a-z]*f)"),
        "会覆盖远程或丢掉未提交的工作，且没有撤销路径",
        globs=("git push --force*", "git push -f*", "git reset --hard*", "git clean -fd*", "git clean -fdx*"),
    ),
    Tripwire(
        "iex",
        "把文本当代码执行",
        _rx(r"\b(iex|invoke-expression)\b"),
        "执行内容在运行前看不见，等同于放弃了审查这一环",
        globs=("iex *", "iex(*", "Invoke-Expression*"),
    ),
    Tripwire(
        "exec-policy",
        "改执行策略或杀软设置",
        _rx(r"\b(set-executionpolicy|set-mppreference|add-mppreference)\b"),
        "为了绕过限制而降低本机防护，改动范围超出本项目",
        globs=("Set-ExecutionPolicy*", "Set-MpPreference*", "Add-MpPreference*"),
    ),
    Tripwire(
        "power-off",
        "关机或重启",
        _rx(r"\b(stop-computer|restart-computer|shutdown)\b"),
        "会中断网关自己——手机上就再也连不上来了",
        globs=("Stop-Computer*", "Restart-Computer*", "shutdown*"),
    ),
    Tripwire(
        "acl-takeown",
        "夺取所有权或改 ACL",
        _rx(r"\b(takeown|icacls)\b"),
        "改的是权限本身，可能让用户自己都进不去那个目录",
        globs=("takeown *", "icacls *"),
    ),
)


def tripwires_file(data_dir: Path) -> Path:
    """额外的绊线配置。

    和 ``blocked_projects.txt`` 用同一套约定（一行一条、``#`` 注释），
    因为"这个文件该长什么样"记住一份就够了。

    每行的格式是 **``名字 = 正则 | glob1, glob2``**。

    那个 ``|`` 后面**不是可选的**，虽然语法上允许省略。原因值得说清楚：
    今天真正拦得住东西的只有 glob（``--disallowedTools``），正则只用来
    在**事后**给这条拒绝命名原因。所以只写正则的规则**拦不住任何东西**——
    它会静默地什么也不做。省略时下面会打一条警告，就是为了让它别静默。
    """
    return data_dir / "tripwires.txt"


def _parse_extra(data_dir: Path) -> list[Tripwire]:
    path = tripwires_file(data_dir)
    if not path.is_file():
        return []
    extra: list[Tripwire] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, body = line.partition("=")
        if not body:
            name, body = f"custom-{lineno}", name
        regex_src, _, glob_src = body.partition("|")
        globs = tuple(g.strip() for g in glob_src.split(",") if g.strip())
        try:
            tripwire = Tripwire(
                name.strip(),
                name.strip(),
                _rx(regex_src.strip()),
                why=f"来自 {path.name}:{lineno}",
                scope="any",
                globs=globs,
            )
        except re.error as exc:
            # 一行写错不该让整个网关起不来——跳过并说清楚是哪一行
            print(f"！{path.name} 第 {lineno} 行的正则无效，已忽略：{exc}")
            continue
        if not globs:
            # 说白了：这条规则不生效。宁可在启动时吵一次，
            # 也不要让用户以为加了一条防线、实际什么也没加。
            print(
                f"！{path.name} 第 {lineno} 行「{name.strip()}」没有 glob，"
                f"声明式通道拦不住它——它只会在别的原因被拒时用来命名原因。"
                f"写成 名字 = 正则 | 命令glob1, 命令glob2 才有拦截效果。"
            )
        extra.append(tripwire)
    return extra


@dataclass
class Verdict:
    """一次判断的结果。**要能解释自己**——手机上的通知得说清拦的是什么、为什么。"""

    decision: Decision
    tripwire: Tripwire | None = None
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def as_payload(self) -> dict:
        """给事件流用的载荷，前端已有的 ``.perm`` 卡片直接读它。"""
        return {
            "decision": self.decision.value,
            "tool_name": None,  # 由调用方补
            "reason": self.reason,
            "tripwire": self.tripwire.as_dict() if self.tripwire else None,
        }


@dataclass
class Policy:
    """权限策略。默认全放，只对绊线做判断。"""

    tripwires: Sequence[Tripwire] = field(default_factory=lambda: DEFAULT_TRIPWIRES)
    #: 命中绊线时的决定。默认 :data:`TRIPWIRE_DECISION`（= 拒绝），
    #: 因为那是今天真的会发生的事——声明式通道没有"等人回答"这个状态。
    #: 想改成"问一下"的前提是先有能把问题送达手机的通道。
    on_tripwire: Decision = TRIPWIRE_DECISION

    def decide(self, tool_name: str, tool_input: dict | None) -> Verdict:
        text = _salient_text(tool_input)
        if not text:
            return Verdict(Decision.ALLOW)

        for tripwire in self.tripwires:
            if tripwire.matches(tool_name, text):
                return Verdict(
                    self.on_tripwire,
                    tripwire=tripwire,
                    reason=f"{tripwire.title}：{tripwire.why}" if tripwire.why else tripwire.title,
                )
        return Verdict(Decision.ALLOW)

    def __len__(self) -> int:
        return len(self.tripwires)


def _salient_text(tool_input: dict | None) -> str:
    """从工具参数里取出"该拿去做匹配的那段文本"。

    优先 ``command``（shell 工具），其次 ``file_path``（文件工具）。
    都没有就把所有字符串值拼起来——宁可多匹配一点也不漏，
    因为绊线只是"停下来问一声"，误报的代价是打扰，漏报的代价是丢东西。
    """
    if not tool_input:
        return ""
    for key in ("command", "script"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    if not any(k in tool_input for k in ("command", "script")):
        parts: list[str] = []
        for value in tool_input.values():
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, Iterable) and not isinstance(value, (bytes, dict)):
                parts.extend(str(v) for v in value)
        return "\n".join(parts)
    return ""


def load_policy(data_dir: Path) -> Policy:
    """内置绊线 + ``data/tripwires.txt`` 里的追加项。

    和项目屏蔽清单同一个做法：内置的保证开箱可用，文件用来加自己的，
    两者是**相加**关系——不能因为用户加了一条就把内置的全关掉，
    那等于让"加一条规则"这个动作意外地拆掉所有防线。
    """
    return Policy(tripwires=(*DEFAULT_TRIPWIRES, *_parse_extra(data_dir)))
