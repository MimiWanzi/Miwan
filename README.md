# Miwan

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)

**离开电脑之后，仍然能用手机发现、下达、盯着你电脑上的 AI Agent。**
*Discover, dispatch, watch and approve the AI agents on your PC — right from your phone.*

这是它的**本地网关**（Local Agent Gateway）：跑在你自己的电脑上，把 Claude Code
这类 Agent 包成一个可远程操作的服务——发现项目、下达任务、实时看它每一步在做什么、
随时中断。手机端只需要一个浏览器，不需要装 App。

> **当前状态：已接入五个 Agent（Claude Code / OpenCode / WorkBuddy / Codex / Qoder），
> 端到端链路可用，且有自动化验收兜着（见[测试与验收](#测试与验收)）。**
> 传输是明文 HTTP —— 这是明确决定推迟的，不是漏掉的，见[已知限制](#已知限制)。

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
| 监控台 | `#/overview` 一屏看所有任务的运行状态（活跃任务可一键中断），主机凭证还能顺手管理设备；每 5 秒自动刷新 |
| 手机扫码连接 | 监控台右侧显示公网地址的二维码，手机扫码即进、网址不用手输；设备页还能出**含配对码**的配对二维码（网关本机发起时），扫完只剩起名+确认（`config.toml` 配 `[server] public_url`；二维码用的 qrcode 已在 requirements.txt） |
| 项目列表 | 列出电脑上被 Agent 用过的项目，含 git 分支与最近活动时间 |
| 新建项目 | 首页手动登记一个绝对路径当项目（目录不存在可顺手创建）——扫描只找得到被 Agent 用过的目录，新开项目全靠它 |
| 项目页 | 历史会话列表 + 新建任务表单（接了多个 Agent 时可挑选由谁执行） |
| 任务页 | **实时事件流**：助手文本、工具调用卡片（含工具名与参数）、工具结果、完成/失败标记；任务结束后可直接在页面底部续接这个会话继续对话（中断/断连的任务也从这里恢复） |
| 会话历史 | 点进历史会话看最近的聊天记录（用户 / 助手 / 工具调用），并直接在会话里续发任务（读正文支持 Claude Code / WorkBuddy / Codex 的会话） |
| 中断 | 一键中断正在跑的任务，状态转为 `cancelled` |
| 断线恢复 | 连接断了自动重连，按 `seq` 补齐，**不重不漏** |
| 探活 | 连接被静默黑洞掉（进隧道、Wi-Fi 转蜂窝）时，约 30 秒内自己发现并重连 |
| 权限通知 | 撞上绊线的操作以**只读卡片**显示在事件流里，写明是哪条绊线、为什么 |
| 微信推送 | 任务完成 / 失败 / 等你批准 / 撞绊线时发一条微信通知——人不在网页上也能知道。设置页选渠道：Server酱（微信扫码拿 SendKey，最省事）、PushPlus、企业微信群机器人；「发一条测试推送」当场验证。fire-and-forget，推送失败绝不影响任务 |
| 设置页 | 首页右上「设置」：推送开关、渠道、key、测试按钮；运行时改的存应用目录下的 `data/notify.json`（优先于 `config.toml [notify]`） |
| 设备管理 | 查看已配对设备，单独吊销（手机丢了不用改根凭证） |

### 电脑上（命令行）

`agents` 检测 Agent 可用性 · `projects` 扫描项目与会话并落库 · `run` 跑真实任务 ·
`tasks` / `replay` 查看与回放 · `serve` 起服务（`--open` 起来就自动开浏览器，exe 场景用）·
`code` / `devices` / `revoke` 管配对与设备 ·
`url` 打主机登录链接（浏览器主机身份丢了时的回头路）。

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
    ├── notify.py       微信推送：任务终态/审批/绊线 → Server酱 / PushPlus / 企业微信
    ├── auth.py         两套凭证：主机 token / 设备凭证
    └── adapters/
          base.py       AgentAdapter 抽象
          claude_code/  Claude Code 实现（发现 / 归一化 / 会话 / 驱动）
                 │
                 ▼  headless
            Claude Code CLI  (cwd = 目标项目路径)
          opencode/     OpenCode 实现（serve 进程共享 / v1 HTTP / SSE）
                 │
                 ▼  HTTP
            opencode serve    (一个网关一个 serve，按目录切项目)
          workbuddy/    WorkBuddy 实现（驱动桌面版内置 CodeBuddy CLI，Anthropic 同族协议）
          codex/        Codex 实现（exec 每回合一个进程 / 沙箱三档映射 / rollout 发现）
          qoder/        Qoder 实现（Anthropic 同族协议复用 Mapper / 云端会话 / credits 计费）
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

**环境要求**：Windows 10/11（本项目的主场——进程树终止、注册表反查、应用目录
位置都绑着它），Python 3.12+。Agent 不求全：本机有哪家 CLI 就能接入哪家
（Claude Code / OpenCode / WorkBuddy / Codex / Qoder），`python -m gateway agents`
会逐个给出检测结果与安装指引。

OpenCode 不是必须的。要接入时：PATH 上有 `opencode` 就自动识别；从源码跑的
（本机就是这样）在 `config.toml` 里配一条启动命令：

```toml
[opencode]
command = ["C:/Users/<你>/.bun/bin/bun.exe", "run", "--cwd", "D:/path/to/opencode/packages/opencode", "src/index.ts"]
```

```bash
pip install -r requirements.txt

# 1. 看看本机有哪些 Agent 可用
python -m gateway agents

# 1b. 有 Agent 明明装了却扫不到？跑环境诊断：
#     打印 doctor 进程实际拿到的 PATH、逐 Agent 的搜索过程与覆盖方法，
#     整段复制发给别人就能求助
python -m gateway doctor

# 2. 扫描项目与会话，落库
python -m gateway projects

# 3. 起网关（会打印配对码）
python -m gateway serve
```

启动后横幅长这样：

```
Miwan · Gateway
────────────────────────────────────────────────────────────────
  本机    http://127.0.0.1:8765/
  局域网  http://192.168.1.23:8765/

  手机上打开上面的地址，输入这个配对码：

          681 158          ← 15 分钟内有效，只能配一台设备
```

手机连同一个 Wi-Fi，打开局域网那个地址，输入配对码即可。
人就在电脑前的话，也可以直接：

```
python -c "from gateway.config import load_config; c = load_config(); print(f'http://127.0.0.1:{c.port}/?token={c.token}')"
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

**手机从外网连接**（人不在家里 Wi-Fi 内也能用）：`config.toml` 里配 `[server] public_url`
指向一条 Cloudflare 临时隧道（`cloudflared tunnel --url http://localhost:8765`），
监控台右侧的二维码就会变成公网地址，手机扫码即进。临时隧道断线后旧网址会永久作废
（手机报 Error 1016 / 530），**双击项目根目录的「重连隧道.cmd」**即可一键恢复：
换新网址 → 更新配置 → 重启网关，一次完成；电脑重启后的冷启动也是双击它。
隧道组件（cloudflared）没装也不用管——脚本会自动从 Cloudflare 官方发布页下载到
应用目录（约 40MB，只需一次），下载失败时会给出手动安装的指引。

重连后手机还是连不上？按顺序查两件事：应用目录 `config.toml` 里的 `public_url`
是否已变成脚本打印的新网址（旧版脚本只会"替换"已有行，配置里本来没有这一行时
替换会静默落空、文件原样写回——二维码会一直停在局域网地址）；手机是否**重新
扫码**——旧地址永久作废，不是"等等就能通"。

想先不碰真实 API 看看界面，可以跑一个只读任务：

```bash
python -m gateway run --prompt "读一下 README.md，用一句话说这个项目做什么" --autonomy read_only
```

### 打包成 exe（给不用命令行的人）

```cmd
build_exe.cmd
```

产物 `dist\Miwan.exe`，单文件、双击就跑：自动在本机浏览器打开控制台
（带主机登录链接，不用手抄 token），命令行参数与 `python -m gateway` 完全一致
（比如 `Miwan.exe serve --port 8765`）。配置和数据统一放在 **`%LOCALAPPDATA%\Miwan\`**——
和 `python -m gateway` 读的是同一份，两种启动方式看到的项目、配对、历史完全一样；
exe 拷到哪个文件夹都连同一份数据。给别人用：对方机器首次运行自动生成全新配置
（想换位置设环境变量 `MIWAN_HOME`）。

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

同一网段的人抓包能拿到 cookie 和配对时的码。这是作者 2026-09-30 明确决定推迟的，不是遗漏。

**还债时点是明确的**：只要还想做 PWA，就必须上 HTTPS——service worker 只在
secure context 里可用。所以做 PWA 之前这件事必须还，不能一直拖。

### 2. "问"是逐 Adapter 的能力事实，不是统一保证

MCP 审批通道**已建成并实测**（真 CLI：批准→真的执行、拒绝→真的不执行）：
Claude Code（`--permission-prompt-tool`）与 Qoder（越界确认路由）能在任务中途
挂起，等你在手机上点允许/拒绝。

但这不是平台对全体的承诺——各 CLI 有没有运行时审批拦截点是厂商事实：
OpenCode / WorkBuddy / Codex 仍是 deny-only（`permission_gate=False`，前端按
能力声明如实展示）。绊线的"拦"半边在审批门处统一生效（Claude/Qoder）；
Codex 的 FULL 档例外——exec 模式没有拦截点，绊线注入不了（已声明的局限）。

### 3. 绊线是正则文本匹配，不是沙箱

它拦得住"模型顺手用了个危险命令"，**拦不住存心绕过的**——字符串拼接、
Base64 解码后 `iex`、`-join` 拼参数，任何正则清单都拦不住。

所以它的定位是**绊线**：碰到了就停下来说一声。真正的边界是 `AutonomyPolicy`
（结构性限权）和将来可能的容器/低权限账户。把正则清单当成安全边界比没有清单更危险，
因为它给人虚假的安心。

### 4. 主机 token 不过期、不可吊销

应用目录下的 `data/token.txt`（`%LOCALAPPDATA%\Miwan\data\`）是根凭证。改它不影响已配对的设备凭证（两套是独立的），
但持有它的人等于持有这台机器。

### 5. 配对限流的状态在内存里

重启清零，且假设"单进程、单实例"。多开一个网关进程，每个各有一份失败计数，
限流就从 5 次变成 5×N 次。本地闭环阶段成立。

**限流是前提不是优化**：6 位码只有 20 bit 熵，配上无限次尝试是纸糊的；
配上 5 次尝试，猜中概率是 10⁻⁶ 量级。将来若有人"优化"掉限流，配对的强度会一起塌掉。

### 6. 主机 token 可以走 URL 查询串

为了"从聊天软件里点开链接直接进"这个手机场景，token 的查找顺序是
查询串 → 请求头 → cookie。带 token 的 URL 会留在浏览器历史里（网关日志在本机）。
局域网内可接受；**别用端口转发把 8765 直接暴露到公网**——传输安全的还债时点
在 Phase 5（HTTPS/WSS），在那之前远程访问走 Cloudflare 隧道（公网侧 TLS 在
边缘终结）或干脆留在局域网。

### 7. OpenCode 的会话正文读不了，非 git 目录扫不到

OpenCode 已接入（`opencode serve` + v1 HTTP，探针与端到端验证齐全），但有三个已知缺口：

- **会话正文读不了**：点进 OpenCode 的历史会话只看到提示，看不到聊天记录
  （Claude 的 JSONL 会话文件才解析得出正文）。**续接不受影响**——续接靠
  native_session_id，不带正文也能继续。
- **非 git 目录的会话扫不到**：OpenCode 把不是 git 仓库的目录一律挂到 `global`
  兜底项目下，没法按项目枚举。所以临时目录、随手跑的目录不会出现在项目列表里
  （探针目录正好被这条挡掉），但也意味着在这种目录里的真实工作要续接得靠任务页。
- **档位跟着会话走**：OpenCode 的权限规则集在**建会话时**一次性交给它，
  之后改不了。给一个会话续接任务时沿用的是创建时的档位边界，中途换档不生效。

另外，Agent 的后端模型由各家自己的配置决定（作者实测机把 Claude Code 和 OpenCode
都指向了 DeepSeek）：非官方模型时 Claude Code 的 stderr 会报 `unrecognized_model`
（`apiKeySource: none` 组合实测见过）。功能基本可用，但这是**未验证依赖**。

### 8. 其他

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

- [ ] **Git 集成**：在项目页只读展示 `status` / `diff` / `log`
- [ ] **并发与任务队列**：多任务排队、每项目并发上限、成本上限（`--max-budget-usd` 已探明可用）
- [x] **接入第二个 Agent**：OpenCode 已接入（serve + v1 HTTP + SSE，含端到端验证），
      抽象层没有因此改动核心——"新增 Agent = 新增子包 + 注册"成立。
      Codex 也已接入（2026-10：`codex exec` 每回合一个进程、`-s` 沙箱三档映射、
      resume 追加同文件，核心零改动；非交互模式无权限提示环节，
      `permission_gate=False` 是能力事实不是待办）
- [ ] **OpenCode 会话正文**：接 `GET /session/:id/message`，把历史会话页补齐

**工程债：**

- [ ] 配对限流状态挪到库里，解除单实例假设
- [ ] 用 `--settings` / `--setting-sources` 隔离用户配置，让 `--permission-mode`
      的档位断言真正成立
- [ ] `probes/` 里几个重的验收脚本共享同一套起停脚手架，减少重复

## 目录结构

```
Miwan/
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
├── build_exe.cmd                    打包单文件 exe（产物 dist\Miwan.exe）
├── config.example.toml              配置模板（复制进应用目录改名为 config.toml）
├── 重连隧道.cmd / reconnect_tunnel.ps1   Cloudflare 临时隧道断线后一键重连
├── 产品介绍书.md                     一页纸产品介绍（能力 / 对比 / 路线）
└── （运行时数据不在仓库里：%LOCALAPPDATA%\Miwan\data\，token 与 SQLite 库都在这）
```

前端是**零依赖、无构建**的：三个静态文件，改完刷新浏览器就是。这样手机端不需要
打包链，也没有"构建产物和源码不一致"的问题。

## 安全须知

- **运行数据目录绝不外传**：`%LOCALAPPDATA%\Miwan\data\` 里有 `token.txt`
  （主机根凭证）和 `gateway.db`（含设备凭证的哈希）。仓库里的 `data/` 是历史遗留，
  同样别提交。
- 网关默认监听 `0.0.0.0`，**整个局域网可达**。在咖啡厅、公司网络这类环境里要么
  换 `--host 127.0.0.1`，要么确保你能接受同网段的人访问这个页面。
- 手机丢了 / 设备丢了：`python -m gateway devices` 找到它，`python -m gateway revoke <id>`。
- 怀疑根凭证泄露：删掉应用目录下的 `data/token.txt` 重启网关会重新生成（已配对的
  设备不受影响，因为设备凭证是独立的）。

## 文档

| 文档 | 内容 |
|---|---|
| `CLAUDE.md` | AI 修改代码必须遵守的工程规则 |
| `docs/roadmap.md` | 接下来建设什么（按代码现状标注各阶段完成度） |
| `docs/architecture.md` | 系统为什么这么设计、模块如何协作、术语约定 |
| `docs/product.md` | 产品定位：用户是谁、解决什么问题、怎么演进 |
| `docs/security-notes.md` | 两套凭证怎么设计的、哪里还敞着、以及踩过的坑 |
| `docs/adapter-extension-notes.md` | 接新 Agent 之前必读：各家协议差异与实测结论 |

## 参与

- **Issue**：Agent 扫不到、某家 CLI 协议变更、真机适配问题，都欢迎开；环境类问题请先跑
  `python -m gateway doctor`，把输出整段贴上来
- **PR**：新 Agent 的 Adapter（**先读 `docs/adapter-extension-notes.md`**，那里有各家协议
  差异与实测结论）、探针补强、文档纠错。接入流程与验收标准见 `CLAUDE.md`：
  能力探针 → Adapter 子包 → 注册 → E2E，能力声明（`AgentCapabilities`）必须如实

## 许可

MIT。见 [LICENSE](LICENSE)。
