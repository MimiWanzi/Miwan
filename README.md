# Remote Agent Workspace

离开电脑之后，仍然能用手机发现、下达、盯着你电脑上的 AI Agent。

这是它的**本地网关**（Local Agent Gateway）：跑在你自己的电脑上，把 Claude Code
这类 Agent 包成一个可远程操作的服务——发现项目、下达任务、实时看它每一步在做什么、
随时中断。手机端只需要一个浏览器，不需要装 App。

> **当前状态：MVP-0，第 0–8 层已完成。**
> 端到端链路可用，且有自动化验收兜着（见[测试与验收](#测试与验收)）。
> 目前只接入了 Claude Code 一个 Agent；传输是明文 HTTP —— 这是明确决定推迟的，
> 不是漏掉的，见[已知限制](#已知限制)。

---

## 它解决什么问题

无人值守的 `claude -p` 有两个绕不开的坑，这个项目就是为了填它们：

1. **看不见。** Agent 在跑什么、调了哪个工具、写没写文件，全在它自己的输出流里。
   把输出归一化成结构化事件、落库、按 `seq` 推给手机，才谈得上"看着它干活"。
2. **权限没人应答。** `--help` 原文说得很清楚，权限提示必须有人回答；
   没人回答等于什么都干不了。所以问题不是"要不要加拦截层"，而是"这道题我们怎么答"。

围绕这两条，剩下的都是确定性的工程量。

## 现在能做什么

### 手机上（浏览器）

| 能力 | 说明 |
|---|---|
| 配对 | 输入 6 位配对码换一份**本设备专属**凭证，不用把主机 token 抄到手机上 |
| 项目列表 | 列出电脑上被 Agent 用过的项目，含 git 分支与最近活动时间 |
| 项目页 | 历史会话列表 + 新建任务表单 |
| 任务页 | **实时事件流**：助手文本、工具调用卡片（含工具名与参数）、工具结果、完成/失败标记 |
| 中断 | 一键中断正在跑的任务，状态转为 `cancelled` |
| 断线恢复 | 连接断了自动重连，按 `seq` 补齐，**不重不漏** |
| 探活 | 连接被静默黑洞掉（进隧道、Wi-Fi 转蜂窝）时，约 30 秒内自己发现并重连 |
| 权限通知 | 撞上绊线的操作以**只读卡片**显示在事件流里，写明是哪条绊线、为什么 |
| 设备管理 | 查看已配对设备，单独吊销（手机丢了不用改根凭证） |

### 电脑上（命令行）

`agents` 检测 Agent 可用性 · `projects` 扫描项目与会话并落库 · `run` 跑真实任务 ·
`tasks` / `replay` 查看与回放 · `serve` 起服务 · `code` / `devices` / `revoke` 管配对与设备。

`run` 和 `replay` 是配成一对的：`run` 出来的每条事件都必须能在库里按原顺序一模一样地
放出来——这正是断线恢复所依赖的那个查询。

## 架构

```
手机 / 桌面浏览器  (web/ 静态文件由网关托管)
    │   HTTP + WebSocket   (token 校验)
    ▼
Local Gateway  (Python 3.12 / aiohttp)
    ├── api.py          REST：projects / sessions / tasks / devices
    ├── ws.py           WebSocket：实时推送 + 按 seq 补发
    ├── events.py       EventBus + 归一化事件类型
    ├── store.py        SQLite：任务状态与事件持久化（标准库 sqlite3，不引 ORM）
    ├── runner.py       任务生命周期状态机
    ├── permissions.py  权限通道：绊线判定（纯函数）
    ├── auth.py         两套凭证：主机 token / 设备凭证
    └── adapters/
          base.py       AgentAdapter 抽象
          claude_code/  Claude Code 实现（发现 / 归一化 / 会话 / 驱动）
                 │
                 ▼  headless
            Claude Code CLI  (cwd = 目标项目路径)
```

**分层原则**：`runner` / `permissions` / `adapters` 只依赖 `EventBus` 和 `Store`，
不 import 任何 aiohttp 或 WebSocket 相关的东西。把局域网直连换成云隧道时，业务层零改动。

## 三条决定了其他一切的设计

**库是唯一事实来源，事件总线只是"有事发生了"的唤醒信号。**
收到唤醒后不是把队列里那条事件发出去，而是拿 `last_seq` 去库里查 `seq > last_seq`。
于是断线重连和实时推送走的是同一段代码，不会出现"补发的逻辑改了、实时的忘了改"；
总线为慢消费者丢事件时也不会静默丢数据。附带好处：**WebSocket 只是优化，不是必需**，
客户端只靠轮询 `GET /api/tasks/{id}/events?since=N` 也能得到完全一样的事件序列。

**事件带单调递增 `seq` 持久化。** `events.id` 自增即 `seq`，免费获得补发与回放能力。

**权限默认全放 + 极短绊线。** 默认档是 `full`：`--allowedTools` 按名放行，
用 `--disallowedTools` 挂绊线。绊线只收"**做了就收不回来**"的操作——
判断标准是能不能撤销，不是看起来危不危险。默认八条：删注册表、递归/强制删除、
磁盘分区操作、强推或改写 git 历史、`iex`、改执行策略或杀软设置、关机重启、夺取所有权/改 ACL。

## 快速开始

**环境要求**：Python 3.12+，本机装有 Claude Code CLI（`claude` 在 PATH 上）。

```bash
pip install -r requirements.txt

# 1. 看看本机有哪些 Agent 可用
python -m gateway agents

# 2. 扫描项目与会话，落库
python -m gateway projects

# 3. 起网关（会打印配对码）
python -m gateway serve
```

启动后横幅长这样：

```
Remote Agent Workspace · Gateway
────────────────────────────────────────────────────────────────
  本机    http://127.0.0.1:8765/
  局域网  http://192.168.1.23:8765/

  手机上打开上面的地址，输入这个配对码：

          681 158          ← 15 分钟内有效，只能配一台设备
```

手机连同一个 Wi-Fi，打开局域网那个地址，输入配对码即可。
人就在电脑前的话，也可以直接：

```
python -c "from pathlib import Path; print('http://127.0.0.1:8765/?token=' + Path('data/token.txt').read_text().strip())"
```

**端口被占**时网关会明确报错并建议换一个，不会先打印一个用不了的配对码：

```bash
python -m gateway serve --port 8766
```

**配对码不见了**（横幅滚过去了、或过期了）：

```bash
python -m gateway code --port 8766
```

配对码只活在 `serve` 进程的内存里，不落库、重启即换，所以这条命令必须去问正在跑的那个
进程——它连不上就会直接说"服务没跑"，而不是给你一个永远配不上的假码。

想先不碰真实 API 看看界面，可以跑一个只读任务：

```bash
python -m gateway run --prompt "读一下 README.md，用一句话说这个项目做什么" --autonomy read_only
```

## 测试与验收

验收方式是**端到端脚本**，不是单元测试——因为这个项目真正会出错的地方都在接缝上
（事件抽取、断线恢复、权限拦截、凭证流转）。

```bash
python probes/verify_adapter.py        # Adapter 与事件归一化
python probes/verify_api.py            # REST 接口
python probes/verify_auth.py           # 配对、凭证、吊销、限流
python probes/verify_permissions.py    # 绊线判定（纯函数，穷举）
python probes/verify_full_autonomy.py  # 全自主档：能干活、且绊线真的拦得住
python probes/verify_reconnect.py      # 断线补发（服务端这半）
python probes/verify_shutdown.py       # 关停时不留孤儿 claude 子进程
python probes/verify_web.py            # 真实无头 Chrome 跑完整条链路（最重的一个）
node   probes/verify_liveness.mjs      # 前端探活逻辑（确定性、免费、毫秒级）
```

最近一次运行：`verify_web.py` 44/44、`verify_reconnect.py` 54/54、`verify_liveness.mjs` 36/36。

> ⚠️ `verify_web.py` / `verify_reconnect.py` 等会**真实调用 API 并消耗额度**，
> 且会在 `~/.claude/projects/` 下产生真实 session 文件。`verify_liveness.mjs` 用
> Node 直接驱动前端逻辑，不碰网络也不花钱，适合改完前端立刻跑一遍。

`probes/probe_claude.py` 是最初的能力探针（第 0 层），它当时推翻了几个想当然的假设，
那些结论写在源码注释里（比如 Windows 上 shell 工具叫 `PowerShell` 而不是 `Bash`，
写死 `--tools Bash` 会静默拿到空工具集）。

## 已知限制

这些都是**已知且明确接受**的，不是待发现的 bug。每条都写清楚了还债的时点。

### 1. 传输是明文 HTTP，局域网内可被嗅探

同一网段的人抓包能拿到 cookie 和配对时的码。用户 2026-09-30 明确决定推迟。

**还债时点是明确的**：只要还想做 PWA，就必须上 HTTPS——service worker 只在
secure context 里可用。所以做 PWA 之前这件事必须还，不能一直拖。

### 2. 权限通道只能"拦"，不能"问"

今天的行为是**绊线命中 → 当场拒绝 + 回合结束时通知手机**。

"先挂起、等人在手机上点允许"这个中间态**做不到**：声明式通道（`--disallowedTools`）
拦下来就是当场拒，CLI 没有"等人回答"这个状态。要做到"问"，得让 `--permission-prompt-tool`
指向一个 MCP server 由网关接管回答——CLI 收这个参数，但 `--help` 里没有，属于未验证依赖，
按和排除 `--bg` 系列同样的理由没上。

**代价很具体**：拒绝记录在 `result.permission_denials` 里，只有回合结束才拿得到。
一个跑 10 分钟的任务，你在第 10 分钟才知道它撞了绊线。

### 3. 绊线是正则文本匹配，不是沙箱

它拦得住"模型顺手用了个危险命令"，**拦不住存心绕过的**——字符串拼接、
Base64 解码后 `iex`、`-join` 拼参数，任何正则清单都拦不住。

所以它的定位是**绊线**：碰到了就停下来说一声。真正的边界是 `AutonomyPolicy`
（结构性限权）和将来可能的容器/低权限账户。把正则清单当成安全边界比没有清单更危险，
因为它给人虚假的安心。

### 4. 主机 token 不过期、不可吊销

`data/token.txt` 是根凭证。改它不影响已配对的设备凭证（两套是独立的），
但持有它的人等于持有这台机器。

### 5. 配对限流的状态在内存里

重启清零，且假设"单进程、单实例"。多开一个网关进程，每个各有一份失败计数，
限流就从 5 次变成 5×N 次。本地闭环阶段成立。

**限流是前提不是优化**：6 位码只有 20 bit 熵，配上无限次尝试是纸糊的；
配上 5 次尝试，猜中概率是 10⁻⁶ 量级。将来若有人"优化"掉限流，配对的强度会一起塌掉。

### 6. 只接了 Claude Code，且本机后端不是 Anthropic 官方

Codex 和 OpenCode 的接入做过源码级调研（结论在 `docs/adapter-extension-notes.md`），
**但没有实测验证过**。抽象层的形状能装下它们，真去接的时候仍要重新走一遍探针。

另外本机 Claude Code 的后端是 `deepseek-flash`、`apiKeySource: none`，
stderr 会报 `unrecognized_model`。功能基本可用，但这是**未验证依赖**。

### 7. 其他

- **没有 PWA / 通知推送**：被"没有 HTTPS"阻塞（service worker 需要 secure context）
- **没有任务队列**：并发提交多个任务时的排队、限流未做
- **单进程假设**：`serve` 假定只有一个实例，`recover()` 会把上次遗留的非终态任务
  标为 `disconnected`（不会自动重跑）
- **Cookie 没有 `Secure` 标志**：故意的，明文 HTTP 下开了浏览器直接不收这个 cookie
- **`--permission-mode` 的隔离没做**：本机 `~/.claude/settings.json` 的实际配置是
  **全放行**，所以"加了 flag 也没坏事"这类结论全是无效的——分不清是 flag 生效还是
  用户自己的配置在生效。要断言某个档位有效，必须先能把用户配置隔离掉，这一条还没做

## 待优化 / 下一步

**优先级较高的：**

- [ ] **给 `run_server` 的绑定路径补探针**。目前"端口被占 → 不打印配对码、退出码 1"
      这条只有手工验证过，`probes/` 里没有任何脚本覆盖它
- [ ] **手机真机局域网实测**。目前所有自动化验收跑的都是桌面版无头 Chrome，
      真机上的 Safari / 国产浏览器内核、软键盘、横竖屏都还没验过
- [ ] **HTTPS**。是 PWA、通知推送两个功能的前置条件

**候选的下一层：**

- [ ] **Git 集成**：在项目页只读展示 `status` / `diff` / `log`（原型书 Phase 9）
- [ ] **并发与任务队列**：多任务排队、每项目并发上限、成本上限（`--max-budget-usd` 已探明可用）
- [ ] **接入第二个 Agent**：Codex 或 OpenCode，用来检验抽象层是不是真的够用
      （调研笔记里的两个缺口都指向权限通道，见 `docs/adapter-extension-notes.md` 末尾）

**工程债：**

- [ ] 配对限流状态挪到库里，解除单实例假设
- [ ] 用 `--settings` / `--setting-sources` 隔离用户配置，让 `--permission-mode`
      的档位断言真正成立
- [ ] `probes/` 里几个重的验收脚本共享同一套起停脚手架，减少重复

## 目录结构

```
RemoteAgent/
├── gateway/            后端（见上方架构图）
├── web/                前端：index.html / app.js / style.css，零依赖无构建
├── probes/             探针与验收脚本
│   ├── verify_*.py     端到端验收
│   ├── probe_*.py      最初的能力探针
│   ├── scratch/        探针的临时工作目录（gitignore）
│   └── out/            探针的原始样本（gitignore）
├── docs/
│   ├── security-notes.md            凭证模型与已知限制
│   └── adapter-extension-notes.md   接新 Agent 之前必读
├── data/               运行时数据：token 与 SQLite 库（gitignore，绝不提交）
└── RemoteAgent项目原型书.md          产品设计文档
```

前端是**零依赖、无构建**的：三个静态文件，改完刷新浏览器就是。这样手机端不需要
打包链，也没有"构建产物和源码不一致"的问题。

## 安全须知

- **`data/` 绝不提交**。里面有 `token.txt`（主机根凭证）和 `gateway.db`（含设备凭证
  的哈希）。已在 `.gitignore` 里，别去掉。
- 网关默认监听 `0.0.0.0`，**整个局域网可达**。在咖啡厅、公司网络这类环境里要么
  换 `--host 127.0.0.1`，要么确保你能接受同网段的人访问这个页面。
- 手机丢了 / 设备丢了：`python -m gateway devices` 找到它，`python -m gateway revoke <id>`。
- 怀疑根凭证泄露：删掉 `data/token.txt` 重启网关会重新生成（已配对的设备不受影响，
  因为设备凭证是独立的）。

## 文档

| 文档 | 内容 |
|---|---|
| `RemoteAgent项目原型书.md` | 产品设计：目标、能力模型、演进阶段 |
| `docs/security-notes.md` | 两套凭证怎么设计的、哪里还敞着、以及踩过的坑 |
| `docs/adapter-extension-notes.md` | 接 Codex / OpenCode 之前必读：三个会让人挂死的冲突 |

## 许可

暂未指定。
