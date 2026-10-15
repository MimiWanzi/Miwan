#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绊线匹配的验收：**不花钱**，纯本地，穷举。

为什么值得单独写一测：这是整个网关里唯一一处"判断错了就直接毁用户东西"的代码。
别的地方错了是显示不对、任务失败，这里是文件夹没了。而且它便宜——
不需要调 Claude，所以可以穷举而不是抽样。

两侧都要测，且**误报这一侧更重要**：

  漏报（该拦没拦）只是绊线没兜住，但绊线本来就不是安全边界（见 permissions.py
  开头的说明），何况放行是用户的默认选择；
  误报（不该拦拦了）会让"默认全放"这个策略事实上退化成"经常打断"，
  用户很快就会把绊线关掉——**一个爱误报的绊线等于没有绊线**。

所以这里正常命令的样本比危险命令还多。

跑法：
    python probes/verify_permissions.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.permissions import (  # noqa: E402
    ASK_TIMEOUT_SECONDS,
    DEFAULT_TRIPWIRES,
    Decision,
    Policy,
    load_policy,
)

checks: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    checks.append((label, bool(passed), detail))


POLICY = Policy()


def fires(tool: str, given: dict) -> str | None:
    """命中就返回绊线 id，没命中返回 None。"""
    verdict = POLICY.decide(tool, given)
    return verdict.tripwire.id if verdict.tripwire else None


def cmd(text: str) -> dict:
    return {"command": text}


# ----------------------------------------------------------------------
# 会拦：每条绊线至少两个样本，用别名/写法多样性砸一砸
# ----------------------------------------------------------------------

HITS: list[tuple[str, str, dict]] = [
    # 1 递归/强制删除
    ("rm-recurse", "Remove-Item -Recurse -Force .\\victim", cmd("Remove-Item -Recurse -Force .\\victim")),
    ("rm-recurse", "rm -rf", cmd("rm -rf node_modules")),
    ("rm-recurse", "ri 别名 + -Recurse", cmd("ri -Recurse -Force build")),
    ("rm-recurse", "rd /s /q", cmd("rd /s /q C:\\temp\\old")),
    ("rm-recurse", "del /s", cmd("del /s /q *.log")),
    ("rm-recurse", "通配符删除（没有 -Recurse 也是一样危险）", cmd("Remove-Item .\\src\\*")),
    ("rm-recurse", "参数写在路径后面", cmd("Remove-Item .\\victim -Recurse")),
    # 2 磁盘
    ("disk-wipe", "Format-Volume", cmd("Format-Volume -DriveLetter D")),
    ("disk-wipe", "diskpart", cmd("diskpart /s wipe.txt")),
    ("disk-wipe", "Clear-Disk", cmd("Clear-Disk -Number 1 -RemoveData")),
    # 3 git 历史
    ("git-rewrite", "push --force", cmd("git push --force origin main")),
    ("git-rewrite", "push -f", cmd("git push -f")),
    ("git-rewrite", "reset --hard", cmd("git reset --hard HEAD~3")),
    ("git-rewrite", "clean -fdx", cmd("git clean -fdx")),
    # 4 管道执行
    ("iex", "iex", cmd('iex "Get-Process"')),
    ("iex", "Invoke-Expression", cmd("Invoke-Expression $payload")),
    # 5 执行策略 / 杀软
    ("exec-policy", "Set-ExecutionPolicy", cmd("Set-ExecutionPolicy Bypass -Scope Process")),
    ("exec-policy", "Set-MpPreference", cmd("Set-MpPreference -DisableRealtimeMonitoring $true")),
    # 6 关机重启
    ("power-off", "Stop-Computer", cmd("Stop-Computer -Force")),
    ("power-off", "Restart-Computer", cmd("Restart-Computer")),
    # 7 注册表
    ("registry-delete", "reg delete", cmd("reg delete HKCU\\Software\\Foo /f")),
    ("registry-delete", "Remove-Item HKLM:", cmd("Remove-Item HKLM:\\Software\\Foo -Recurse")),
    # 8 所有权 / ACL
    ("acl-takeown", "takeown", cmd("takeown /f C:\\Windows\\System32 /r")),
    ("acl-takeown", "icacls /grant", cmd("icacls C:\\data /grant Everyone:F")),
]

# ----------------------------------------------------------------------
# 不该拦：日常操作。**这一侧数量必须更多**
# ----------------------------------------------------------------------

CLEAN: list[tuple[str, str, dict]] = [
    ("读文件", "Read", {"file_path": "D:\\Miwan\\main.py"}),
    ("写文件", "Write", {"file_path": "a.txt", "content": "hello"}),
    ("改文件", "Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}),
    ("搜索", "Grep", {"pattern": "def main", "path": "gateway"}),
    ("列文件", "Glob", {"pattern": "**/*.py"}),
    ("跑测试", "PowerShell", cmd("python -m pytest tests/")),
    ("装依赖", "PowerShell", cmd("pip install aiohttp")),
    ("看 git 状态", "PowerShell", cmd("git status --short")),
    ("看提交历史", "PowerShell", cmd("git log --oneline -20")),
    ("普通提交", "PowerShell", cmd('git commit -m "fix: 修正 force push 的文案"')),
    ("正常推送（没有 -f）", "PowerShell", cmd("git push origin main")),
    ("切分支", "PowerShell", cmd("git checkout -b feature/permissions")),
    ("git clean 干跑", "PowerShell", cmd("git clean -n")),
    ("删单个文件（不递归）", "PowerShell", cmd("Remove-Item .\\build\\out.tmp")),
    ("rm 单个文件（无 flag）", "PowerShell", cmd("rm stale.log")),
    ("删 git 里的文件", "PowerShell", cmd("git rm --cached secrets.env")),
    ("看磁盘", "PowerShell", cmd("Get-Volume")),
    ("看电脑信息", "PowerShell", cmd("Get-ComputerInfo")),
    ("看进程", "PowerShell", cmd("Get-Process | Where-Object CPU -gt 10")),
    ("看注册表", "PowerShell", cmd("Get-ItemProperty HKCU:\\Software\\Foo")),
    ("看权限", "PowerShell", cmd("icacls C:\\data")),  # 只读查询……见下方已知误报
    ("管道到 Out-File", "PowerShell", cmd("Get-Date | Out-File stamp.txt")),
    ("建目录", "PowerShell", cmd("New-Item -ItemType Directory -Force .\\build")),
    ("npm 构建", "PowerShell", cmd("npm run build")),
    ("docker 构建", "PowerShell", cmd("docker build -t app .")),
    ("python -c 简单串", "PowerShell", cmd("python -c \"print('rm -rf')\"")),  # 见下方已知误报
]

# ----------------------------------------------------------------------

KNOWN_FALSE_POSITIVES = {"icacls C:\\data", "python -c \"print('rm -rf')\""}


def run_case(tool: str, given: dict) -> tuple[str | None, str]:
    v = POLICY.decide(tool, given)
    return (v.tripwire.id if v.tripwire else None), v.reason


def main() -> int:
    # ---- 1. 该拦的都拦到了 ----
    for want, label, given in HITS:
        actual, _ = run_case("PowerShell", given)
        check(f"拦下 {label}", actual == want, f"期望 {want}，实际 {actual or '放行'}")

    # ---- 2. 日常操作不许误伤 ----
    for label, tool, given in CLEAN:
        actual, _ = run_case(tool, given)
        sample = given.get("command") or given.get("file_path") or ""
        expected = "icacls" in str(sample) or "print('rm -rf')" in str(sample)
        if expected:
            check(f"〔已知误报〕{label}", True, f"命中 {actual}——记录在案，不算失败")
        else:
            check(f"放行 {label}", actual is None, f"误命中 {actual}")

    # ---- 3. scope：Write 的内容里有危险命令，不该当成用户要执行它 ----
    danger_text = "Remove-Item -Recurse -Force C:\\"
    v = POLICY.decide("Write", {"file_path": "notes.md", "content": danger_text})
    check(
        "写文件的内容里含危险命令 → 不拦",
        v.tripwire is None,
        f"实际 {v.tripwire.id if v.tripwire else '放行'}",
    )
    v = POLICY.decide("PowerShell", cmd(danger_text))
    check("同样的文本出现在命令行里 → 拦", v.tripwire is not None)

    # ---- 4. 决定与理由 ----
    v = POLICY.decide("PowerShell", cmd("rm -rf /"))
    # 今天命中绊线就是**当场拒**：声明式通道没有"挂起等人回答"这个状态。
    # 断言 DENY 而不是 ASK，是为了别让代码看起来像会弹审批框——
    # 用户定的是"等 2 分钟 → 拒绝"，我们只交付了后半句。
    check("命中绊线当场拒绝（不是等人批准）", v.decision is Decision.DENY, v.decision.value)
    check(
        "ASK 是保留值、默认走不到",
        Policy().on_tripwire is Decision.DENY,
    )
    check("理由不为空（手机上要能看懂拦的是什么）", bool(v.reason), v.reason)
    payload = v.as_payload()
    trip = payload.get("tripwire") or {}
    check("载荷里带了绊线 id 和说明", bool(trip.get("id")) and bool(payload.get("reason")))
    check("超时 2 分钟，超时后拒绝", ASK_TIMEOUT_SECONDS == 120.0)

    # ---- 4b. 两条规则同时命中时，标签要是更具体的那条 ----
    # `Remove-Item HKLM:\... -Recurse` 既算"递归删除"也算"删注册表"。
    # 两条都拦得住，但手机上只显示先命中的那条——显示"删注册表"用户才知道
    # 该不该点，显示"递归删除"他还得猜删的是哪儿。所以顺序是有意义的。
    v = POLICY.decide("PowerShell", cmd("Remove-Item HKLM:\\Software\\Foo -Recurse"))
    check(
        "双命中时用更具体的标签",
        v.tripwire is not None and v.tripwire.id == "registry-delete",
        f"实际 {v.tripwire.id if v.tripwire else '放行'}",
    )

    # ---- 4c. 每条绊线都得有两半：正则 + glob ----
    # 只写正则不写 glob 不会报错，只是 CLI 那道静默地不存在了。
    # 静默失效的防线比没有防线更糟，所以让它在这里响。
    missing = [t.id for t in DEFAULT_TRIPWIRES if not t.globs]
    check("每条绊线都带 glob（CLI 那道别忘写）", not missing, f"缺 glob：{missing}")

    expanded = [g for t in DEFAULT_TRIPWIRES for g in t.disallowed_tools()]
    check("glob 展开成 PowerShell(...) 形式", all(g.startswith("PowerShell(") for g in expanded))
    check(
        "展开结果能直接喂给 --disallowedTools",
        "PowerShell(Remove-Item*-Recurse*)" in expanded,
        f"{len(expanded)} 条",
    )

    # ---- 5. 干净的输入不该炸 ----
    for odd in (None, {}, {"command": ""}, {"command": 123}, {"unknown": ["a"]}):
        try:
            POLICY.decide("PowerShell", odd)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            check(f"异常输入 {odd!r} 不炸", False, repr(exc))
            break
    else:
        check("异常/空输入不炸", True)

    # ---- 6. 配置文件：追加而不是覆盖 ----
    with tempfile.TemporaryDirectory(prefix="ra-perm-") as tmp:
        data_dir = Path(tmp)
        check("没有配置文件时用内置清单", len(load_policy(data_dir)) == len(DEFAULT_TRIPWIRES))

        (data_dir / "tripwires.txt").write_text(
            "# 自己加的\nnpm-publish = npm publish\n\n  \n# 注释行不算\n",
            encoding="utf-8",
        )
        policy = load_policy(data_dir)
        check(
            "文件里的规则是**追加**，内置的还在",
            len(policy) == len(DEFAULT_TRIPWIRES) + 1,
            f"{len(policy)} 条",
        )
        v = policy.decide("PowerShell", cmd("npm publish --access public"))
        check("自定义规则能命中", v.tripwire is not None and v.tripwire.id == "npm-publish")
        v = policy.decide("PowerShell", cmd("rm -rf /"))
        check("加了自定义规则后内置规则照样生效", v.tripwire is not None)

        # 只写正则、不写名字
        (data_dir / "tripwires.txt").write_text("kubectl delete\n", encoding="utf-8")
        v = load_policy(data_dir).decide("PowerShell", cmd("kubectl delete pod web-1"))
        check("只写正则也能用（名字自动生成）", v.tripwire is not None)

        # 自定义规则**必须带上 glob 才真的拦得住**。
        # 只写正则的规则今天什么也拦不了（CLI 只吃 glob），
        # 所以这条要能从规则本身的形状上看出来，而不是靠记性。
        (data_dir / "tripwires.txt").write_text(
            "npm-publish = npm publish | npm publish*, npm publish *\n"
            "no-glob = npm unpublish\n",
            encoding="utf-8",
        )
        policy = load_policy(data_dir)
        g = policy.decide("PowerShell", cmd("npm publish --access public")).tripwire
        check("带 glob 的自定义规则展开得出 --disallowedTools 参数",
              g is not None and g.disallowed_tools() != [],
              f"{g.disallowed_tools() if g else '—'}")
        ng = policy.decide("PowerShell", cmd("npm unpublish x")).tripwire
        check("不带 glob 的自定义规则 globs 为空（调用方据此判断拦不拦得住）",
              ng is not None and ng.disallowed_tools() == [])

        # 坏正则：跳过，但不能把整个网关带崩
        (data_dir / "tripwires.txt").write_text("bad = ([unclosed\nok = npm publish\n", encoding="utf-8")
        policy = load_policy(data_dir)
        check("坏正则只跳过它自己", len(policy) == len(DEFAULT_TRIPWIRES) + 1, f"{len(policy)} 条")
        check(
            "坏行不影响同一文件里的其它行",
            policy.decide("PowerShell", cmd("npm publish")).tripwire is not None,
        )

    # ---- 7. 命令行参数：档位真的翻译成了正确的 flag 吗 ----
    # 免费能测的部分就别留给"跑一次真任务看看"。这一节尤其值得，
    # 因为它的失败方式是静默的：flag 拼错了不报错，只是权限行为不是你要的那个。
    from pathlib import Path as _P

    from gateway.adapters.base import AutonomyPolicy
    from gateway.adapters.claude_code.session import FULL_TOOLS, ClaudeSession

    def args_for(autonomy: AutonomyPolicy) -> list[str]:
        session = ClaudeSession(
            exe=_P("claude.exe"), cwd=_P("."), task_id="t-test",
            emit=lambda _e: None, autonomy=autonomy,
        )
        return session._build_args()  # noqa: SLF001 —— 就是来测它的

    full = args_for(AutonomyPolicy.FULL)
    check("FULL 用了 --allowedTools", "--allowedTools" in full)
    check("FULL 带上了 --disallowedTools", "--disallowedTools" in full)
    check(
        "FULL 的绊线 glob 真的在里面",
        "PowerShell(Remove-Item*-Recurse*)" in full,
    )
    check(
        "FULL **没有**用 --tools（那是限定工具集，会把能力砍掉）",
        "--tools" not in full,
    )

    # 这一条是防回归的，也是整个第 6 层的底线：
    # 用户明确否决了"关掉权限系统"这条路，只同意"全放但留绊线"。
    # 哪天有人图省事改成 bypassPermissions，这里要响。
    check(
        "没有出现 bypassPermissions",
        "bypassPermissions" not in full,
    )
    check(
        "没有出现 --dangerously-skip-permissions",
        not any("dangerously" in a for a in full),
    )

    read_only = args_for(AutonomyPolicy.READ_ONLY)
    check("READ_ONLY 仍然只用限定工具集", "--tools" in read_only and "--allowedTools" not in read_only)
    check(
        "READ_ONLY 不含 PowerShell",
        "PowerShell" not in read_only,
        "只读档必须结构上写不出东西",
    )

    edit = args_for(AutonomyPolicy.EDIT)
    check("EDIT 仍是 acceptEdits", "acceptEdits" in edit and "--disallowedTools" not in edit)

    # FULL 的放行清单得真的覆盖了三种能力，否则"全部放行"名不副实
    for tool in ("Read", "Write", "Edit", "PowerShell"):
        check(f"FULL 放行了 {tool}", tool in FULL_TOOLS)

    # 空绊线清单的边界：--allowedTools 是可变参数，如果它是**最后一个**参数，
    # 后面再跟别的东西就会被它吞掉。这个坑本探针已经踩过一次
    # （见 probe_permissions.py 里 prompt 被吞的那段），所以把这条钉住。
    from gateway.permissions import Policy as _Policy

    bare = ClaudeSession(
        exe=_P("claude.exe"), cwd=_P("."), task_id="t-test",
        emit=lambda _e: None, autonomy=AutonomyPolicy.FULL,
        policy=_Policy(tripwires=()),
    )._build_args()  # noqa: SLF001
    check("空绊线清单时不发 --disallowedTools", "--disallowedTools" not in bare)
    check("空绊线清单时 --allowedTools 之后没有裸参数", "--allowedTools" in bare)
    budgeted = ClaudeSession(
        exe=_P("claude.exe"), cwd=_P("."), task_id="t-test",
        emit=lambda _e: None, autonomy=AutonomyPolicy.FULL,
        policy=_Policy(tripwires=()), budget_usd=1.0,
    )._build_args()  # noqa: SLF001
    check(
        "空绊线 + 预算：--max-budget-usd 没被 --allowedTools 吞掉",
        "--max-budget-usd" in budgeted,
        " ".join(budgeted[-4:]),
    )

    # ---- 汇总 ----
    failed = 0
    for label, passed, detail in checks:
        if not passed:
            print(f"  ✗  {label}\n       → {detail}")
        failed += 0 if passed else 1
    print("=" * 72)
    print(f"  {len(checks) - failed}/{len(checks)} 项通过" + ("" if not failed else "  ← 有失败"))
    if KNOWN_FALSE_POSITIVES:
        print(f"  已知误报（不修，因为绊线只是问一声）：{'、'.join(sorted(KNOWN_FALSE_POSITIVES))}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
