"""doctor——把"为什么扫不到 Agent"变成一张可以发给别人看的清单。

动机（2026-10-13，第一个真实案例）：exe 发给朋友，他机器上明明装了
Agent 却一个都扫不到。``agents`` 命令只给结论（●/○/✗），不给过程——
拿到"扫不到"三个字之后，排查只能靠来回猜。典型根源是
**双击 exe 拿到的 PATH 和终端里的 PATH 不是同一份**，这只有把
"doctor 进程实际看到的 PATH、逐目录搜到了什么、where 交叉探针说什么"
都打出来才能一锤定音。

结构（CLAUDE.md 规则 2/10）：本模块只遍历注册表，**不认识任何具体
Agent 的名字**；每个 Agent 搜哪里、有什么已知位置、config 怎么写，
全部由各 Adapter 的 ``diagnose_rows()`` 可选钩子提供
（惯例同 ``adapter_shutdown``：不是所有 Adapter 都需要，靠 getattr 探测）。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from .adapters import PENDING_AGENTS, registered
from .adapters.diagnose import DiagRow, path_entries
from .config import APP_DIR, CONFIG_PATH, load_config

#: agents 命令用 ●/○/✗，doctor 用词不用符号——诊断输出经常被复制粘贴
#: 到聊天工具里，符号在 GBK 控制台或纯文本里都可能丢形。
_STATUS_LABEL = {
    "ready": "[可用]",
    "unconfigured": "[未配置]",
    "missing": "[未找到]",
}

_INDENT = "    "


def _print_rows(rows: list[DiagRow]) -> None:
    for row in rows:
        lines = row.value.splitlines() or [""]
        print(f"{_INDENT}{row.label}")
        for line in lines:
            print(f"{_INDENT}  {line}")


def _print_header() -> None:
    """环境头。排查"扫不到"的第一件事是确认**这个进程**活在哪份环境里。"""
    if getattr(sys, "frozen", False):
        program = f"{Path(sys.executable).name}（exe 打包运行）"
    else:
        program = sys.executable
    print("Miwan Gateway · 环境诊断")
    print(f"{_INDENT}程序      {program}")
    print(f"{_INDENT}Python    {platform.python_version()}  "
          f"（{platform.machine()}）")
    print(f"{_INDENT}系统      {platform.system()} {platform.release()}")
    print(f"{_INDENT}应用目录  {APP_DIR}")


def _print_config() -> None:
    print(f"{_INDENT}配置文件  {CONFIG_PATH}")
    try:
        load_config()
    except SystemExit as exc:
        # 配置坏了不该挡住诊断——locate 在 config 缺席时会回退到 PATH 搜索
        print(f"{_INDENT}          解析失败：{exc}")
        return
    from .config import loaded_config  # load_config 成功后才有值

    config = loaded_config()
    assert config is not None
    source = f"（来源：{config.source}）" if config.source else "（不存在——全部用默认值）"
    print(f"{_INDENT}          {source}")
    exists = "存在" if config.data_dir.is_dir() else "不存在（首次运行时创建）"
    print(f"{_INDENT}数据目录  {config.data_dir}（{exists}）")


def _print_path() -> None:
    entries = path_entries()
    print()
    print(f"PATH（本进程实际拿到的，共 {len(entries)} 项）"
          f"——双击 exe 启动时，这就是 Explorer 给的，可能和终端里的不一样：")
    if not entries:
        print(f"{_INDENT}（空）")
        return
    for index, entry in enumerate(entries, 1):
        print(f"{_INDENT}{index:>2}. {entry}")


def run() -> int:
    """打印整份诊断。只读不写、零网络请求、零额度。"""
    _print_header()
    _print_config()
    _print_path()

    print()
    print("Agent 逐个诊断：")
    for cls in registered():
        print()
        print(f"── {cls.display_name}（{cls.agent_id}）")
        try:
            info = cls.detect()
        except Exception as exc:  # noqa: BLE001 —— 与 detect_all 同款的刻意兜底
            print(f"{_INDENT}状态  [检测异常] {type(exc).__name__}: {exc}")
            continue
        label = _STATUS_LABEL.get(info.status.value, "[?]")
        line = f"{_INDENT}状态  {label} {info.status.value}"
        if info.version:
            line += f"  版本 {info.version}"
        print(line)
        if info.executable:
            print(f"{_INDENT}可执行  {info.executable}")
        if info.detail:
            print(f"{_INDENT}详情  {info.detail}")
        hook = getattr(cls, "diagnose_rows", None)
        if hook is None:
            continue
        try:
            _print_rows(hook())
        except Exception as exc:  # noqa: BLE001 —— 诊断自身坏了不拖垮其余 Agent
            print(f"{_INDENT}诊断行生成失败：{type(exc).__name__}: {exc}")

    print()
    if PENDING_AGENTS:
        print(f"尚未接入（不在检测范围）：{'、'.join(PENDING_AGENTS)}")
    print()
    print("排查到这里就有的放矢了：状态 [未找到] 看该 Agent 的 PATH 搜索和已知位置；")
    print("[未配置] 看详情里的登录/版本原因；都正常但派活失败，再来找开发者。")
    print("把这份输出整段复制发给别人，比口头描述「装了但扫不到」有用得多。")
    return 0
