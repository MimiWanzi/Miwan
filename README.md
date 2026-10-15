# Miwan

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)

**Control the AI agents on your PC from your phone** — discover, dispatch, watch, approve.
**在手机上发现、派遣、盯着、审批你电脑上的 AI Agent。** Local-first, no cloud.

English · [中文](#中文)

Miwan is a small local gateway that runs on your own machine and turns CLI AI
agents (Claude Code, Codex, Qoder, CodeBuddy, OpenCode) into a service you can
operate from a phone browser. No app, no account, no vendor cloud.

> Status: five agents wired up and end-to-end tested. Transport is plaintext
> HTTP for now — a deliberate deferral, not an oversight.

<!-- TODO: add a screenshot / GIF of the phone dashboard here — this is the project's best selling point. -->

## Features

- **Phone dashboard** — one screen for all running tasks, one-tap interrupt, no app to install.
- **Live event stream** — assistant text, tool calls, results, completion — normalized into structured events.
- **Reconnect without gaps** — every event is persisted with a monotonic `seq`; reconnecting replays exactly what was missed, no duplicates.
- **Approval gate** — agents with a runtime approval hook (Claude Code / Qoder) hang mid-task until you tap allow/deny on your phone; the rest are deny-only and reported honestly.
- **Tripwires** — a short list of "can't be undone" operations is blocked. A tripwire, not a sandbox.
- **WeChat notifications** — task finished / failed / waiting for approval / tripwire hit.
- **One-file exe** — double-click on Windows, no command line needed.
- **Your data stays local** — SQLite and files on your own disk.

## Supported agents

| Agent | Integration | Runtime approval |
|---|---|---|
| Claude Code | headless CLI + JSONL sessions | ✅ `--permission-prompt-tool` |
| Qoder | Anthropic-family stream-json, cloud sessions | ✅ |
| OpenCode | shared `opencode serve` + v1 HTTP/SSE | ❌ deny-only |
| WorkBuddy (CodeBuddy CLI) | Anthropic-family stream-json | ❌ deny-only |
| Codex | `codex exec` per turn, sandbox tiers | ❌ deny-only |

Adding an agent is an Adapter subpackage + a registry entry — the core does not change.

## Quick start

Requirements: **Windows 10/11**, **Python 3.12+**.

```bash
pip install -r requirements.txt

python -m gateway agents      # what agents are installed on this machine
python -m gateway projects    # scan projects & sessions into the database
python -m gateway serve       # start the gateway (prints a pairing code)
```

Open the local/LAN address on your phone (same Wi-Fi) and enter the 6-digit code.
An agent is installed but not detected? `python -m gateway doctor` prints a
copy-pasteable diagnosis.

Prefer no command line? `build_exe.cmd` produces a single `dist\Miwan.exe` —
double-click it and the dashboard opens.

Remote access (outside your Wi-Fi) uses a Cloudflare tunnel: set
`[server] public_url` in `config.toml`, and run `reconnect_tunnel.ps1`
(「重连隧道.cmd」) to re-establish a dropped tunnel.

## Configuration & data

Everything lives in one app directory: `%LOCALAPPDATA%\Miwan\` (override with
`MIWAN_HOME`):

- `config.toml` — settings (template: `config.example.toml`)
- `data/token.txt` — host root credential
- `data/gateway.db` — SQLite (tasks, events, paired devices)

`config.toml` may contain a token — never commit it. Priority:
CLI flags > `MIWAN_*` env vars > `config.toml` > built-in defaults.

## Testing

Acceptance is end-to-end probes, not unit tests — the things that break live
in the seams (event extraction, reconnect, permission interception, credential flow).

```bash
python probes/verify_adapter.py        # adapter & event normalization
python probes/verify_api.py            # REST API
python probes/verify_auth.py           # pairing, credentials, revoke, rate limit
python probes/verify_permissions.py    # tripwire rules (pure functions)
python probes/verify_full_autonomy.py  # full autonomy: works, and tripwires bite
python probes/verify_reconnect.py      # gap-free replay (server half)
python probes/verify_web.py            # full chain in real headless Chrome (heaviest)
node   probes/verify_liveness.mjs      # frontend liveness — free, instant
```

Some probes (`verify_web.py`, `verify_reconnect.py`, ...) call a real Agent API
and consume quota. `verify_liveness.mjs` does not.

## Contributing

Issues and PRs are welcome — especially new agent adapters, probe hardening,
and real-device feedback. See `CLAUDE.md` for the engineering rules we hold
new adapters to. For a detection problem, run `python -m gateway doctor` and
paste the full output.

## License

MIT. See [LICENSE](LICENSE).

---

## 中文

**在手机上发现、派遣、盯着、审批你电脑上的 AI Agent。** Local-first，不经过任何人的云。

Miwan 是跑在你自己电脑上的本地网关：把 Claude Code 这类 CLI Agent 包成一个可远程
操作的服务——发现项目、下达任务、实时看它每一步在做什么、随时中断。手机端只需要一个
浏览器，不装 App、不注册账号、没有厂商云。

> 当前状态：已接入五个 Agent 并有端到端验收。传输目前是明文 HTTP——这是明确推迟、
> 且已记录的决定，不是遗漏。

### 能做什么

- **手机控制台** — 一屏看所有任务状态、活跃任务一键中断，无需装 App
- **实时事件流** — 助手文本、工具调用、结果、完成/失败，归一化成结构化事件
- **断线不丢** — 事件带单调递增 `seq` 持久化，重连按序精确补发，不重不漏
- **审批门** — 有运行时审批钩子的 Agent（Claude Code / Qoder）会中途挂起等你手机上点允许/拒绝；其余如实标 deny-only
- **绊线** — 拦截"做了就收不回来"的操作。是绊线，不是沙箱
- **微信推送** — 任务完成 / 失败 / 等你批准 / 撞绊线
- **单文件 exe** — Windows 双击即用，不碰命令行
- **数据在本地** — SQLite 与文件都在你自己的磁盘上

### 支持的 Agent

| Agent | 接入方式 | 运行时审批 |
|---|---|---|
| Claude Code | headless CLI + JSONL 会话 | ✅ `--permission-prompt-tool` |
| Qoder | Anthropic 同族 stream-json / 云端会话 | ✅ |
| OpenCode | 共享 `opencode serve` + v1 HTTP/SSE | ❌ deny-only |
| WorkBuddy（CodeBuddy CLI） | Anthropic 同族 stream-json | ❌ deny-only |
| Codex | `codex exec` 每回合进程 / 沙箱三档 | ❌ deny-only |

新增 Agent = 新增 Adapter 子包 + 注册，核心不动。

### 快速开始

要求：**Windows 10/11**、**Python 3.12+**。

```bash
pip install -r requirements.txt

python -m gateway agents      # 本机有哪些 Agent 可用
python -m gateway projects    # 扫描项目与会话落库
python -m gateway serve       # 起网关（打印配对码）
```

手机连同一 Wi-Fi，打开局域网地址、输入 6 位配对码即可。装了却扫不到？
`python -m gateway doctor` 会打印可整段复制的诊断。

不想碰命令行：`build_exe.cmd` 打包出单文件 `dist\Miwan.exe`，双击自动打开控制台。

外网访问（Wi-Fi 之外）：`config.toml` 配 `[server] public_url` 指向 Cloudflare 隧道；
隧道断线后双击「重连隧道.cmd」（`reconnect_tunnel.ps1`）一键恢复。

### 配置与数据

统一在应用目录 `%LOCALAPPDATA%\Miwan\`（`MIWAN_HOME` 可覆盖）：

- `config.toml` — 配置（模板 `config.example.toml`）
- `data/token.txt` — 主机根凭证
- `data/gateway.db` — SQLite（任务、事件、已配对设备）

`config.toml` 可能含 token，**别提交**。优先级：命令行 > `MIWAN_*` 环境变量 > `config.toml` > 默认。

### 测试与验收

验收是端到端探针，不是单元测试——会出错的地方都在接缝上（事件抽取、断线恢复、权限拦截、凭证流转）。

```bash
python probes/verify_adapter.py        # Adapter 与事件归一化
python probes/verify_api.py            # REST 接口
python probes/verify_auth.py           # 配对、凭证、吊销、限流
python probes/verify_permissions.py    # 绊线判定（纯函数）
python probes/verify_full_autonomy.py  # 全自主档：能干活且绊线真的拦得住
python probes/verify_reconnect.py      # 断线补发（服务端这半）
python probes/verify_web.py            # 真实无头 Chrome 跑完整条链路（最重）
node   probes/verify_liveness.mjs      # 前端探活逻辑——免费、毫秒级
```

其中 `verify_web.py` / `verify_reconnect.py` 等会真实调用 Agent API、消耗额度；
`verify_liveness.mjs` 不碰网络。

### 参与

欢迎 Issue / PR：新 Agent 适配、探针补强、真机反馈。新增 Adapter 的工程规则见 `CLAUDE.md`。
扫不到 Agent 时先跑 `python -m gateway doctor`，把输出整段贴上来。

### 许可

MIT，见 [LICENSE](LICENSE)。
