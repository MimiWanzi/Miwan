# CLAUDE.md — AI 修改代码必须遵守的规则

Miwan：Local-First 的个人 Agent Control Plane（手机统一发现/派遣/观察/审批电脑上的 AI Agent）。
Python 3.12 / aiohttp / SQLite，前端 web/ 三个静态文件零构建。

## 工程规则

1. **修改代码前先检查现有实现**——先 Grep/Read，再动手；不重复实现已有能力
2. **Agent 差异必须通过 Adapter 隔离**（`gateway/adapters/`），业务层只认识 `AgentAdapter` / `AgentInfo` / `ScanResult`
3. **Agent 原始输出必须在 Adapter 层归一化**为统一事件，核心不认识各 Agent 的原始协议
4. **Store（`gateway/store.py`）是数据真相**，EventBus 只是唤醒信号
5. **WebSocket 只是实时传输优化，不是真相**——纯轮询 `?since=N` 必须得到完全一致的事件序列
6. **Event 必须可恢复**：单调递增 `seq` 落库，断线补发与实时推送走同一段查询
7. **新 Agent 必须如实声明 Capabilities**（`gateway/adapters/base.py` 的 `AgentCapabilities`），标 True 前必须有依据
8. **新 Agent 必须完成 E2E 验证**：能力探针 → Adapter 子包 → Registry 注册 → E2E 探针（烧额度层 `probes/verify_*.py`；零额度层探针为本地辅助脚本，不入库）
9. **不允许为假设中的未来需求提前抽象**（例：不要建 Transport 抽象层，等第二种真实传输产生重复代码再说）
10. **不允许单个 Agent 的特殊逻辑污染 Core**：runner/api/permissions 里不允许出现 Agent 名字特判
11. **修改数据库必须考虑 Migration**：走 `store.py` 的 `_migrate()` 补列惯例，不推倒重建
12. **保持现有 Agent 正常工作**：每次接入/重构后，已有 Agent 的探针必须仍然全绿
13. **修改前优先复用现有 Gateway / Registry / Session / Event 能力**
14. **第三方 Agent / Plugin 是可执行代码不是配置文件**，必须考虑执行权限与信任边界
15. **Remote 场景必须考虑 HTTPS/WSS**（本地开发 HTTP/WS 可接受）
16. 如果现有抽象无法优雅接入第三个 Agent，**先修抽象，再继续接入**

## 架构验收指标

不问"支持多少 Agent"，问：**新增一个完全不同的 Agent，核心代码改动是多少？**
理想答案：新增 Adapter 子包 + Registry 注册 + 探针，Core 基本不动。

## 术语

代码/API 里 **Device = 控制端（手机）**，**Host = 被控电脑**（"主机 token"即 Host 根凭证），
Agent/Project/Session/Task/Event 依次为被管理的 Agent、工作目录、会话、任务、事件。

## 文档职责（不要互相复制）

对外门面是 `README.md`。更详细的设计 / 产品 / 路线说明只保留在作者本地、
不进公开仓库，仅供本地 AI 与作者参考——不要把它们的内容复制进 README。

## 速查

- 配置 + 数据：`%LOCALAPPDATA%\Miwan\`（与启动方式无关；`MIWAN_HOME` 可覆盖）
- 验收：`python probes/verify_api.py` 等（烧真实额度）；改前端先跑 `node probes/verify_liveness.mjs`（免费）
- exe 打包：`build_exe.cmd` → `dist\Miwan.exe`
