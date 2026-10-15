/* Keeper — 第 5 层前端（原生 ES 模块，无构建步骤）
 *
 * 三条约束决定了这里几乎所有写法：
 *
 * 1. **没有构建步骤。** 原生模块 + 原生 DOM。给一个局域网页面引入 npm 和打包器，
 *    等于给这个项目加第二条工具链，而它换来的东西这里一样用不上。
 *
 * 2. **只用 textContent，绝不用 innerHTML。** 事件流里装的是 Agent 读到的文件内容、
 *    命令输出、报错文本——来源是本机上任意文件。用 innerHTML 就等于让
 *    "Agent 读到的一个恶意 HTML 文件"获得在手机上执行脚本的能力。
 *    这不是理论风险：Agent 的任务之一就是到处读文件。
 *
 * 3. **事件的唯一事实来源是库，不是 WebSocket。** 页面重新打开时 since=0，
 *    服务端把库里的事件从头补发一遍——所以"关掉页面再打开、事件不丢"
 *    不需要前端存任何状态。只有断线重连才用 since=lastSeq。
 *
 * 路由用 hash（#/p/xxx），因为服务端只把 index.html 挂在 / 上，
 * 深一点的真实路径会 404。hash 顺带白拿了前进/后退。
 */

// ----------------------------------------------------------------------
// DOM 小工具
// ----------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

/** 建元素。
 *
 * `text` 是唯一的文本入口，走 textContent。没有 `html` 参数是**故意的**：
 * 只要这个口子存在，早晚会有人图省事把 Agent 的输出塞进去。
 */
function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

let toastTimer = null;
function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 4000);
}

const short = (s, n) => (s.length > n ? s.slice(0, n) + "…" : s);

function relTime(ts) {
  if (!ts) return "";
  const delta = Date.now() / 1000 - ts;
  if (delta < 60) return "刚刚";
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  if (delta < 86400 * 30) return `${Math.floor(delta / 86400)} 天前`;
  return new Date(ts * 1000).toLocaleDateString("zh-CN");
}

function clockTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "";
}

function fmtDuration(ms) {
  if (!ms && ms !== 0) return "";
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

function fmtCost(usd) {
  if (usd === null || usd === undefined) return "";
  return `$${usd.toFixed(4)}`;
}

const STATUS = {
  created:           { label: "已创建", cls: "" },
  queued:            { label: "排队中", cls: "" },
  running:           { label: "运行中", cls: "live" },
  waiting_approval:  { label: "等待批准", cls: "warn" },
  completed:         { label: "已完成", cls: "ok" },
  failed:            { label: "失败", cls: "err" },
  cancelled:         { label: "已中断", cls: "" },
  disconnected:      { label: "已断开", cls: "warn" },
};

const TERMINAL = new Set(["completed", "failed", "cancelled", "disconnected"]);

// Agent 的展示名：权威在 /api/agents 的 name（跟着 adapter 走），这里缓存一份
// 给项目页/任务页用。内置映射是第一屏的兜底——detect 要起子进程，比首屏慢。
const agentLabels = { claude_code: "Claude Code", codex: "Codex", opencode: "OpenCode", qoder: "Qoder", workbuddy: "WorkBuddy" };
function primeAgentLabels(payload) {
  for (const a of (payload && payload.agents) || []) {
    // /api/agents 的字段是 AgentInfo.id（不是 agent_id），曾写错导致 priming 永远落空
    if (a.id && a.name) agentLabels[a.id] = a.name;
  }
}
const agentLabel = (id) => agentLabels[id] || id || "";

function statusPill(status) {
  const s = STATUS[status] || { label: status || "?", cls: "" };
  return el("span", { class: `pill ${s.cls}`.trim(), text: s.label });
}

// ----------------------------------------------------------------------
// API
// ----------------------------------------------------------------------

/** 配对界面要显示的原因（"被吊销了" / "没凭证"），由下面那条 401 分支设置。 */
let pairNotice = "";

async function api(path, options) {
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch { /* 非 JSON 响应：下面按状态码报错 */ }
  if (!resp.ok) {
    const err = new Error((data && data.detail) || (data && data.error) || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.code = data && data.error;

    // 401 = 这份凭证不被接受了（没有 / 不对 / 被吊销）。这不是"加载失败"——
    // 重试一万次也还是 401。唯一有意义的事是回配对界面。
    //
    // **但配对接口自己的 401 不算**：那是"配对码不对"，属于配对界面内部的
    // 一次失败，该把原因显示在表单上。当成掉凭证处理的话，页面会在用户
    // 每输错一次码时重刷一遍，把刚输入的内容也一起清掉。
    if (resp.status === 401 && path !== "/api/pair") {
      err.reauth = true;
      // "被吊销"是有故事的一件事（用户自己或别人主动停用了这台设备），
      // 原样转达；单纯"没凭证"没有故事，说人话就行——
      // 别把 token 这种词端到刚打开页面的用户面前。
      pairNotice = err.code === "revoked"
        ? (data.detail || "这台设备已被吊销，需要重新配对。")
        : "这台设备还没有配对过。";
    }
    throw err;
  }
  return data;
}

const postJSON = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

// ----------------------------------------------------------------------
// 顶栏
// ----------------------------------------------------------------------

function setTitle(text) { $("#title").textContent = text; }

function setBack(href) {
  const back = $("#back");
  if (!href) { back.hidden = true; return; }
  back.hidden = false;
  back.onclick = () => { location.hash = href; };
}

function setConn(state, label) {
  const conn = $("#conn");
  if (!state) { conn.hidden = true; return; }
  conn.hidden = false;
  conn.className = state;
  conn.querySelector("b").textContent = label;
}

// ----------------------------------------------------------------------
// 事件流渲染
// ----------------------------------------------------------------------

/** 从工具入参里挑出最有信息量的一行。
 *
 * 按工具名挑字段，而不是把整个 input 打出来：手机上要一眼看到"在读哪个文件"，
 * 而不是一屏 JSON。认不出的工具退回键值对——加第二个 Agent 时也不会空白。
 */
const ARG_FIELD = {
  Read: "file_path", Write: "file_path", Edit: "file_path",
  NotebookEdit: "notebook_path", MultiEdit: "file_path",
  Glob: "pattern", Grep: "pattern", LS: "path",
  PowerShell: "command", Bash: "command",
  WebFetch: "url", WebSearch: "query", Task: "description",
};

function summarize(name, input) {
  if (!input || typeof input !== "object") return "";
  const field = ARG_FIELD[name];
  if (field && input[field]) return short(String(input[field]), 160);
  const keys = Object.keys(input);
  if (!keys.length) return "";
  return short(keys.map((k) => `${k}=${String(input[k])}`).join(" "), 160);
}

function resultBlock(payload) {
  const text = payload.content || "（无输出）";
  const first = text.split("\n").find((line) => line.trim()) || "（空）";
  return el(
    "details",
    { class: `result${payload.is_error ? " err" : ""}` },
    el("summary", { text: short(first.trim(), 120) }),
    el("pre", { text }),
  );
}

function textBlock(text) {
  const wrap = el("div", { class: "ev text" });
  // 只认围栏代码块这一种标记。完整 markdown 需要一个解析库，
  // 而它带来的解析器是又一个要信任的东西——为一个局域网页面不划算。
  const parts = String(text).split("```");
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const nl = part.indexOf("\n");
      const body = (nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, "");
      wrap.append(el("pre", { class: "code", text: body }));
    } else if (part.trim()) {
      wrap.append(el("div", { class: "para", text: part.trim() }));
    }
  });
  return wrap;
}

function metaLine(tag, detail, extra) {
  return el(
    "div", { class: "ev meta-line" },
    el("span", { class: "tag", text: tag }),
    detail ? ` ${detail}` : "",
    extra ? el("div", { text: extra }) : null,
  );
}

/** 把一条归一化事件变成 DOM。
 *
 * 返回 null 表示"已经处理进别处了"（工具结果会被塞进它那张调用卡片里）。
 */
function renderEvent(event, toolCards, append, permCards) {
  const p = event.payload || {};
  switch (event.type) {
    case "task_started":
      return metaLine("▶", p.model ? `开始 · ${p.model}` : "开始", p.cwd);

    case "assistant_text":
      return textBlock(p.text);

    case "tool_call": {
      const head = el(
        "div", { class: "call-head" },
        el("span", { class: "tool", text: p.name || "?" }),
        el("span", { class: "arg", text: summarize(p.name, p.input), title: JSON.stringify(p.input ?? {}) }),
      );
      const card = el("div", { class: "ev call" }, head, el("div", { class: "result" }));
      if (p.id) toolCards.set(p.id, card);
      return card;
    }

    case "tool_result": {
      const host = p.tool_use_id ? toolCards.get(p.tool_use_id) : null;
      if (host) {
        // 结果并进调用卡片：手机上不用在两条记录之间来回找对应关系。
        // 这也是重连时"从结果开始补发"（调用那条已被跳过）不会出错的原因——
        // host 找不到就退化成下面这条独立渲染。
        const slot = host.querySelector(".result");
        slot.append(resultBlock(p));
        if (p.is_error) host.classList.add("err");
        append(host, true);   // true = 已经在文档里了，只需要滚一下
        return null;
      }
      return el("div", { class: "ev" }, resultBlock(p));
    }

    case "permission_request": {
      // 审批通道发来的「等人批准」（runner 的 request_permission）：
      // 带 permission_id + source=prompt，要渲染成可以点是/否的卡片。
      if (p.source === "prompt" && p.permission_id) {
        const card = el(
          "div", { class: "ev perm ask", "data-perm": String(p.permission_id) },
          el("div", { text: `⏸ 等你批准：${p.tool_name || "?"}` }),
          p.tool_input
            ? el("div", { class: "mono", text: short(JSON.stringify(p.tool_input), 300) })
            : null,
        );
        const disable = () => {
          for (const b of card.querySelectorAll("button")) b.disabled = true;
        };
        const decide = async (decision) => {
          disable();
          try {
            await postJSON(`/api/permissions/${p.permission_id}/decision`, { decision });
            // 结果会以 permission_decided 事件回来，卡片内容由那里统一换——
            // 这里不就地改文案，避免补发重放时出现两处真相
          } catch (err) {
            failure(err);
            for (const b of card.querySelectorAll("button")) b.disabled = false;
          }
        };
        card.append(
          el("div", { class: "actions" },
            el("button", { class: "primary", text: "允许", onclick: () => decide("allow") }),
            el("button", { class: "danger", text: "拒绝", onclick: () => decide("deny") }),
          ),
        );
        if (permCards) permCards.set(String(p.permission_id), card);
        return card;
      }
      // source 有三种，**历史的也得认**：第 6 层之前落库的是 "no_host"，
      // 之后落 "tripwire" / "not_allowed"。而库是唯一事实来源，
      // 补发会把老事件原样送回来——只认新值的话，翻看旧任务时那行会变成空白，
      // 看起来像前端坏了，实际是我们在改协议时忘了这一条。
      const why =
        p.source === "tripwire"
          ? `绊线：${p.reason || (p.tripwire && p.tripwire.title) || "危险操作"}`
          : p.source === "not_allowed"
            ? p.reason || "这个工具没有被放行，而无人应答的权限提示等于拒绝"
            : p.source === "no_host"
              ? "当时还没有权限通道，操作被自动拒绝"
              : p.reason || "";
      return el(
        "div", { class: "ev perm" },
        el("div", { text: `⚠ 操作被拒：${p.tool_name || "?"}` }),
        p.tool_input ? el("div", { class: "mono", text: short(JSON.stringify(p.tool_input), 200) }) : null,
        why ? el("div", { text: why }) : null,
      );
    }

    case "permission_decided": {
      // 把对应的「等你批准」卡片换成结果。翻旧任务时（从头补发）卡片区
      // 先建后换，顺序天然正确；直接落到 decided 事件而没有卡片的极端情况
      // （比如 decided 比卡片还早到达）退化为一条静态行，信息不丢。
      const card = permCards && permCards.get(String(p.permission_id));
      if (card) {
        card.replaceChildren(
          el("div", { text: p.decision === "allow"
            ? "✓ 已允许（手机上批准）"
            : p.auto
              ? "✗ 已自动拒绝（长时间无人答复）"
              : "✗ 已拒绝（手机上否决）" }),
        );
        card.classList.toggle("ask-allowed", p.decision === "allow");
        card.classList.toggle("ask", p.decision !== "allow");
        return null;
      }
      return metaLine(
        p.decision === "allow" ? "✓" : "✗",
        p.decision === "allow" ? "权限请求已批准" : "权限请求已拒绝",
        "",
      );
    }

    case "task_completed":
      return metaLine(
        "✓", "完成",
        [fmtDuration(p.duration_ms), fmtCost(p.cost_usd), p.num_turns ? `${p.num_turns} 轮` : ""]
          .filter(Boolean).join(" · "),
      );

    case "task_failed":
      return el(
        "div", { class: "ev perm" },
        el("div", { text: `✗ 失败 · ${p.reason || "未知"}` }),
        p.error ? el("div", { class: "mono", text: short(String(p.error), 400) }) : null,
      );

    default:
      return el("div", { class: "ev raw" }, el("pre", { class: "code", text: JSON.stringify(p).slice(0, 2000) }));
  }
}

// ----------------------------------------------------------------------
// WebSocket
// ----------------------------------------------------------------------

/** 订阅一个任务的事件流，断了自动重连。
 *
 * 重连**必须带 since=lastSeq**：否则每次抖动都会把整个历史重发一遍，
 * 而手机进电梯出来一次可能要重连三次。
 */
function openStream(taskId, { since = 0, onEvent, onTask, onConn, onFatal }) {
  let lastSeq = since;
  let closed = false;
  let attempts = 0;
  let ws = null;        // 当前这条连接。被放弃的旧连接不再是它，回调据此自我忽略
  let timer = null;     // 重连退避用的定时器
  let pingTimer = null; // 探活用的定时器
  let openTimer = null; // 建连期限用的定时器
  let lastRx = 0;       // 最近一次**收到任何帧**的时刻
  let lastPing = 0;     // 最近一次**发出 ping** 的时刻

  // ------------------------------------------------------------------
  // 探活：为什么非有不可
  //
  // 连接被**明确关掉**（RST、对端进程退出）时浏览器会触发 onclose，
  // 重连逻辑自然启动——那条路已经验过。但连接被**黑洞掉**时不一样：
  // 手机进隧道、Wi-Fi 转蜂窝的那一瞬间会静默丢包，TCP 不报错，
  // onclose 要等系统的连接超时（可能几分钟），界面上就一直写着"已连接"，
  // 而事件流其实已经静止了，用户根本不知道该刷新。
  //
  // 所以主动问：定期发 ping，服务端回 pong（gateway/ws.py 的协议文档里
  // 有这一对）。**超过 STALE_MS 一帧都没收到，就当作这条连接已经死了。**
  //
  // 服务端那个 30 秒的协议层 ping 救不了这个场景，两重原因：浏览器在
  // 协议层自动回 pong，JS 完全看不见；而且它是**服务端**在探客户端，
  // 探不出"客户端发的包到不了服务端"。
  // ------------------------------------------------------------------

  /** 多久主动问一次。 */
  const PING_MS = 10000;
  /** 多久没听到任何东西就判死（连着三次 ping 都没回音）。 */
  const STALE_MS = 30000;

  // 两个数都往"宁可早判"这边取。判错的代价只是重连一次，而重连是
  // 幂等的（带 since=lastSeq，重复的帧按 seq 去重）——反过来，判晚了的
  // 代价是用户盯着一个静止的屏幕以为任务卡住了。STALE_MS 和服务端
  // WS_HEARTBEAT 同量级（30 秒）。

  function stopPing() {
    clearInterval(pingTimer);
    pingTimer = null;
  }

  function startPing(sock) {
    stopPing();
    // 按 PING_MS/3 跑，而不是 PING_MS：判死的时刻才不会被定时器的抖动
    // 往后拖，最多多等几秒而不是多等一整个周期。
    pingTimer = setInterval(() => {
      if (sock !== ws) { stopPing(); return; } // 这条已经被换掉了
      // 页面在后台时**不判死**：浏览器会节流甚至冻住定时器，一条健康的连接
      // 也可能几十分钟轮不到一次检查，这时候按"多久没消息"判死就是误杀。
      //
      // 回到前台时那条连接多半真的已经死了（手机锁屏期间系统会收走 socket），
      // 所以回来的第一次检查通常就会判死重连——这是**有意**的，不是误判：
      // 重连很便宜（带 since=lastSeq，只补缺失的那几条），而停在一个
      // 看着像连接着的死连接上，用户什么都不知道。最多 3 秒后就会动手。
      if (document.hidden) return;

      const now = Date.now();
      if (now - lastRx > STALE_MS) { abandon(); return; }
      // 刚听到过就别问了：任务在跑的时候事件帧本身就是"对面还活着"的证据，
      // 再叠一层 ping 纯属多余（一个正在刷屏的事件流上，ping 一次都不会发）。
      if (now - lastRx < PING_MS) return;
      // 但节流要按"上次**发**ping 的时刻"，不能按 lastRx：ping 没人回的时候
      // lastRx 是不动的，拿它当节流依据会让每一次 tick 都再发一个，
      // 那 STALE_MS 就成了"发了十次没回音"，跟注释里的三次数不上。
      if (now - lastPing < PING_MS) return;
      lastPing = now;
      try {
        sock.send(JSON.stringify({ type: "ping" }));
      } catch {
        // 发不出去（socket 已经在关了）不用管，下一轮 age 变大一样会判死
      }
    }, PING_MS / 3);
  }

  /** 判定当前这条连接已死，换一条。
   *
   * **不等 `onclose`。** 黑洞掉的连接上 `close()` 要等 TCP 自己超时才会
   * 触发 onclose——那正是这里想绕开的等待。做法是直接把指针移开：
   * 旧 socket 的回调都会发现 `sock !== ws` 而自我忽略，所以它后面不管
   * 什么时候响，都不会打断新连接，也不会把重连排第二遍。
   */
  function abandon() {
    const dead = ws;
    ws = null;
    stopPing();
    try { dead.close(); } catch { /* 已经无所谓了 */ }
    scheduleReconnect();
  }

  function scheduleReconnect() {
    if (closed) return;
    clearTimeout(timer);
    const delay = Math.min(500 * 2 ** attempts, 8000);
    attempts += 1;
    onConn("off", `重连中 ${Math.round(delay / 1000)}s`);
    timer = setTimeout(connect, delay);
  }

  function connect() {
    if (closed) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const url = `${scheme}://${location.host}/ws?task_id=${encodeURIComponent(taskId)}&since=${lastSeq}`;
    const sock = new WebSocket(url);
    ws = sock;
    // 建的那一刻就算"刚听到过"。不这么写的话 lastRx 还停在 0，
    // 探活定时器第一次跑起来时 age 是个天文数字，一上来就判死。
    // lastPing 同理：连上时服务端紧接着就发 hello，不必马上再问一次。
    lastRx = Date.now();
    lastPing = lastRx;

    // 握手也要有期限。TCP 连上了但 WebSocket 的 upgrade 响应回不来
    // （半开的连接、要登录的公共 Wi-Fi 门户、被丢包的黑洞）是**没有
    // onclose 的**：探活定时器要 onopen 之后才启动，所以这种连接会永远
    // 卡在"重连中"，等不到任何一次重试。这里给它一个和判死同一个期限。
    clearTimeout(openTimer);
    openTimer = setTimeout(() => {
      if (sock === ws && sock.readyState !== WebSocket.OPEN) abandon();
    }, STALE_MS);

    sock.onopen = () => {
      if (sock !== ws) return;
      clearTimeout(openTimer);
      attempts = 0;
      onConn("on", "已连接");
      startPing(sock);
    };

    sock.onmessage = (msg) => {
      if (sock !== ws) return; // 已经被放弃的旧连接，晚到的帧一律丢掉
      lastRx = Date.now();     // 任何帧都算"对面还活着"，pong 也算

      let frame;
      try { frame = JSON.parse(msg.data); } catch { return; }

      if (frame.type === "event") {
        // 去重：重连与实时推送可能重叠，seq 是单调的，比它小的都收过
        if (frame.event.seq <= lastSeq) return;
        lastSeq = frame.event.seq;
        onEvent(frame.event);
      } else if (frame.type === "task") {
        onTask(frame.task);
      } else if (frame.type === "hello") {
        onTask(frame.task);
      } else if (frame.type === "error") {
        // 服务端说明原因后就会关闭。这时候重连只会无限循环——停掉。
        closed = true;
        clearTimeout(openTimer);
        stopPing();
        onFatal(frame.error);
      }
      // pong 走到这里就够了：上面的 lastRx 已经记下了
    };

    sock.onclose = () => {
      if (sock !== ws) return; // 主动放弃的那条，重连已经排过，别排第二次
      if (closed) return;
      stopPing();
      scheduleReconnect();
    };

    sock.onerror = () => { /* 后面必跟 onclose，重连逻辑放那里 */ };
  }

  connect();
  return {
    close() {
      closed = true;
      clearTimeout(timer);
      clearTimeout(openTimer);
      stopPing();
      if (ws) ws.close();
    },
  };
}

// ----------------------------------------------------------------------
// 路由
// ----------------------------------------------------------------------

/** 离开当前视图时要做的事（关掉 WebSocket 等）。 */
let teardown = () => {};
/** 事件流是否吸在底部。用户往上翻时不能把他拽回去。 */
let stick = true;

$("#view").addEventListener("scroll", () => {
  const view = $("#view");
  stick = view.scrollHeight - view.scrollTop - view.clientHeight < 48;
});

async function route() {
  teardown();
  teardown = () => {};
  setConn(null);

  const view = $("#view");
  view.replaceChildren(el("div", { class: "empty", text: "载入中…" }));
  view.scrollTop = 0;
  // 显式复位，不指望 scrollTop=0 触发的那次 scroll 事件：上一个视图往上翻过之后
  // stick 是 false，而 viewTask 是异步的——不复位的话新任务的事件流不会自动跟随。
  stick = true;

  const hash = location.hash.replace(/^#/, "") || "/";
  const parts = hash.split("/").filter(Boolean);

  try {
    if (parts[0] === "pair") await viewPair();
    else if (parts[0] === "devices") await viewDevices();
    else if (parts[0] === "settings") await viewSettings();
    else if (parts[0] === "overview") await viewOverview();
    else if (parts[0] === "p" && parts[2] === "s" && parts[3])
      await viewSession(decodeURIComponent(parts[1]), decodeURIComponent(parts[3]));
    else if (parts[0] === "p" && parts[1]) await viewProject(decodeURIComponent(parts[1]));
    else if (parts[0] === "t" && parts[1]) await viewTask(decodeURIComponent(parts[1]));
    else await viewProjects();
  } catch (err) {
    // 凭证问题不是错误页能解决的事，直接送去配对界面。
    // 这里 `return` 而不是继续渲染：hashchange 会再进来一次，
    // 先画一个"加载失败"再跳走只会闪一下。
    if (err.reauth) { go("#/pair"); return; }

    setTitle("出错了");
    setBack(null);
    view.replaceChildren(
      el("div", { class: "empty" },
        el("div", { text: `加载失败：${err.message}` }),
        el("div", { style: "margin-top:12px" },
          el("button", { text: "重试", onclick: () => route() })),
      ),
    );
  }
}

function go(href) { location.hash = href; }

function failure(err) {
  setConn("bad", "已断开");
  toast(err.message || String(err));
}

// ----------------------------------------------------------------------
// 视图：配对
// ----------------------------------------------------------------------

/** 把输入规整成 `123 456`。
 *
 * 横幅上显示的就是这个带空格的形状，用户很可能照着连空格一起抄过来；
 * 而服务端比的是 ``"123456"``。不管的话，"照着屏幕上抄"这个最自然的动作
 * 会稳定失败，而错误信息（"配对码不对"）会把人引去怀疑自己看错了数字。
 */
function formatCode(raw) {
  const digits = raw.replace(/\D/g, "").slice(0, 6);
  return digits.length > 3 ? `${digits.slice(0, 3)} ${digits.slice(3)}` : digits;
}

async function viewPair() {
  setTitle("配对");
  setBack(null);

  const view = $("#view");
  const notice = pairNotice;
  pairNotice = "";   // 只显示一次：用户看过之后就轮到表单说话了

  const code = el("input", {
    type: "text", inputmode: "numeric", autocomplete: "one-time-code",
    maxlength: 7, placeholder: "000000", class: "code-input",
  });
  code.addEventListener("input", () => { code.value = formatCode(code.value); });

  // 扫码配对：电脑端出的二维码里就带着配对码（/?pair=123456），扫进来直接填好。
  const prefill = (new URLSearchParams(location.search).get("pair") || "").replace(/\D/g, "");
  const autoFilled = prefill.length === 6;
  if (autoFilled) code.value = formatCode(prefill);

  const name = el("input", {
    type: "text", placeholder: "设备名，比如「我的手机」", maxlength: 40,
  });

  const submit = el("button", { class: "primary", text: "配对" });
  const message = el("div", { class: "pair-msg", hidden: true });

  const form = el("form", { class: "pair-form" },
    el("label", { text: "配对码" }),
    code,
    el("label", { text: "设备名（自己看的，随便起）" }),
    name,
    submit,
  );

  // 主机登录：浏览器的"主机"身份只认主机 token，配对码换不来它。
  // 没有这个入口的话，凭证一丢（换浏览器、清缓存、30 天过期）就没有回头路了。
  const tokenInput = el("input", {
    type: "password", placeholder: "主机凭证（token）", autocomplete: "off",
  });
  const hostBtn = el("button", { class: "ghost", text: "主机登录" });
  const hostMsg = el("div", { class: "pair-msg", hidden: true });
  hostBtn.onclick = async () => {
    const token = tokenInput.value.trim();
    if (!token) return;
    hostBtn.disabled = true;
    try {
      await api(`/api/whoami?token=${encodeURIComponent(token)}`);
      go("#/");
    } catch {
      hostMsg.textContent =
        "这份主机凭证不对。它放在网关那台电脑的 data\\token.txt 里，" +
        "或者在那台机器上跑 `python -m gateway url --open` 拿一条免输入的登录链接。";
      hostMsg.hidden = false;
      hostBtn.disabled = false;
    }
  };

  form.onsubmit = async (e) => {
    e.preventDefault();
    const digits = code.value.replace(/\D/g, "");
    message.hidden = true;
    if (digits.length !== 6) {
      message.textContent = "配对码是 6 位数字。";
      message.hidden = false;
      return;
    }

    submit.disabled = true;
    submit.textContent = "配对中…";
    try {
      // 这一条**必须**走 api() 而不是 fetch：cookie 是服务端在这次响应里
      // 直接种下的，浏览器收下即可——前端从头到尾没碰过那个 token。
      await postJSON("/api/pair", { code: digits, name: name.value.trim() });
      go("#/");
    } catch (err) {
      // 配额用尽时服务端会带 Retry-After，但更直接的信号是它自己写好的那句话
      // （"尝试次数太多，请等 N 分钟再试"）。原样显示，不再翻译一遍。
      message.textContent = err.message || String(err);
      message.hidden = false;
      submit.disabled = false;
      submit.textContent = "配对";
      code.focus();
      code.select();
    }
  };

  view.replaceChildren(
    ...(notice ? [el("div", { class: "banner", text: notice })] : []),
    ...(autoFilled
      ? [el("div", { class: "banner", text: "配对码已从二维码自动填好——起个设备名，点「配对」就行。" })]
      : []),
    el("div", { class: "pair-intro" , text:
      "网关那台电脑的启动横幅上有一个 6 位配对码。把它输进来，这台设备就拿到了自己的凭证——" +
      "以后不用再输，也不会在网址里带着长期凭证。" }),
    form,
    message,
    el("p", { class: "hint", text: "配对码 15 分钟内有效，而且只能配一台设备。" }),
    el("div", { class: "section-title", text: "这台电脑的主人？" }),
    el("p", { class: "hint", text:
      "用主机凭证登录，才能用设备管理和监控台的二维码。" +
      "凭证在网关那台电脑的 data\\token.txt，或在那台机器上跑 `python -m gateway url --open`。" }),
    tokenInput,
    hostBtn,
    hostMsg,
  );

  (autoFilled ? name : code).focus();
}

// ----------------------------------------------------------------------
// 视图：设备管理
// ----------------------------------------------------------------------

/** `refresh` 在吊销成功后调用，让宿主视图自己决定怎么重画（设备页整页重刷，监控台只刷一遍数据）。 */
function fmtDevice(d, refresh = viewDevices) {
  const row = el("div", { class: `card flat${d.revoked ? " off" : ""}` },
    el("div", { class: "name", text: d.name }),
    el("div", { class: "path", text: d.id }),
    el("div", { class: "sub" },
      d.revoked
        ? el("span", { class: "pill err", text: "已吊销" })
        : el("span", { class: "pill ok", text: "有效" }),
      el("span", { text: `配对于 ${relTime(d.created_at) || "—"}` }),
      el("span", { text: d.last_seen_at ? `最近使用 ${relTime(d.last_seen_at)}` : "从未使用" }),
    ),
  );
  if (!d.revoked) {
    const btn = el("button", { class: "danger", text: "吊销", style: "margin-top:8px" });
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = "吊销中…";
      try {
        await postJSON(`/api/devices/${encodeURIComponent(d.id)}/revoke`, {});
        toast("已吊销，那台设备需要重新配对");
        await refresh();
      } catch (err) {
        failure(err);
        btn.disabled = false;
        btn.textContent = "吊销";
      }
    };
    row.append(btn);
  }
  return row;
}

/** 在 box 里出「含配对码」的配对二维码，btn 是触发的按钮（负责 loading 态）。
 *
 * 把配对码直接包进二维码（/?pair=xxxxxx），手机扫完只剩起名+确认。
 * 安全口径不变：配对码只发给**网关本机**发起的请求（api.py 里那条规矩），
 * 所以远程打开页面拿不到码，退回"扫网址 + 手输横幅上的码"。
 * 地址三级兜底：公网地址 → 网关算出的局域网地址 → 当前页面的地址。
 */
async function renderPairQr(box, btn) {
  const idle = () => { btn.disabled = false; btn.textContent = "手机扫码配对"; };
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const [issued, me] = await Promise.all([
      postJSON("/api/pairing-code", {}),
      api("/api/whoami"),
    ]);
    const base = me.public_url || me.lan_url || location.origin;
    if (/^https?:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(base)) {
      idle();
      box.replaceChildren(el("p", { class: "hint", text:
        "现在只有本机回环地址，手机扫这个码打不开。确认手机和电脑连同一个 Wi-Fi 后重试，" +
        "或去 config.toml 配 [server] public_url。" }));
      return;
    }
    const target = issued.code ? `${base}/?pair=${issued.code}` : base;
    const img = el("img", { src: `/api/qr.svg?text=${encodeURIComponent(target)}`, alt: "配对二维码" });
    const qr = el("div", { class: "qr big" }, img);
    img.addEventListener("error", () => qr.replaceWith(
      el("p", { class: "hint", text: "二维码生成失败：服务端需要 pip install qrcode。" })));
    box.replaceChildren(qr);
    box.append(el("p", { class: "hint", text: issued.code
      ? `手机相机扫码 → 配对码自动填好 → 起个名字点「配对」。码 ${issued.code.slice(0, 3)} ${issued.code.slice(3)}，${Math.round((issued.seconds_left || 0) / 60)} 分钟内有效。`
      : "手机扫码打开页面后，手输网关那台电脑横幅上的 6 位配对码。" }));
    box.append(el("button", { class: "ghost", text: "换一个码", onclick: () => renderPairQr(box, btn) }));
  } catch (err) {
    failure(err);
    idle();
  }
}

async function viewDevices() {
  setTitle("设备");
  setBack("#/");

  const view = $("#view");
  const data = await api("/api/devices");

  const nodes = [];
  const pairing = data.pairing || {};

  const pairBtn = el("button", { class: "primary", text: "手机扫码配对" });
  const pairBox = el("div");
  pairBtn.onclick = () => renderPairQr(pairBox, pairBtn);

  nodes.push(el("div", { class: "card flat" },
    el("div", { class: "name", text: "配一台新手机？" }),
    pairBtn,
    pairBox,
  ));
  nodes.push(el("div", { class: "banner", text: pairing.active
    ? `当前配对码还有 ${Math.round((pairing.seconds_left || 0) / 60)} 分钟有效。看不到它就回网关那台机器上看横幅。`
    : "当前没有有效的配对码。点上面的「手机扫码配对」换一个。" }));

  nodes.push(el("div", { class: "section-title", text: `已配对设备（${data.devices.length}）` }));
  if (!data.devices.length) {
    nodes.push(el("div", { class: "empty", text: "还没有配对过任何设备。" }));
  }
  for (const device of data.devices) nodes.push(fmtDevice(device));

  nodes.push(el("p", { class: "hint", text:
    "吊销是立刻生效的：那台设备下次请求就会被挡下来，需要重新配对。" }));

  view.replaceChildren(...nodes);
}

// ----------------------------------------------------------------------
// 视图：设置
// ----------------------------------------------------------------------

const CHANNEL_INFO = {
  serverchan: { name: "Server酱（微信服务号）", keyHint: "SendKey，在 sct.ftqq.com 微信扫码后复制" },
  pushplus: { name: "PushPlus（微信公众号）", keyHint: "token，在 pushplus.plus 微信扫码后复制" },
  wecom_webhook: { name: "企业微信群机器人", keyHint: "" },
};

async function viewSettings() {
  setTitle("设置");
  setBack("#/");

  const view = $("#view");
  const { settings, channels } = await api("/api/settings/notify");

  const enabled = el("input", { type: "checkbox" });
  enabled.checked = !!settings.enabled;

  const channel = el("select");
  for (const c of channels) {
    const opt = el("option", { value: c, text: (CHANNEL_INFO[c] || { name: c }).name });
    opt.selected = c === settings.channel;
    channel.append(opt);
  }

  const key = el("input", { type: "text", autocomplete: "off" });
  key.value = settings.key || "";
  const webhook = el("input", {
    type: "text", autocomplete: "off",
    placeholder: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…",
  });
  webhook.value = settings.webhook_url || "";

  const keyRow = el("label", {}, "key", key);
  const webhookRow = el("label", {}, "webhook 地址", webhook);
  function syncChannel() {
    key.placeholder = (CHANNEL_INFO[channel.value] || { keyHint: "" }).keyHint;
    keyRow.hidden = channel.value === "wecom_webhook";
    webhookRow.hidden = channel.value !== "wecom_webhook";
  }
  channel.onchange = syncChannel;
  syncChannel();

  const payload = () => ({
    enabled: enabled.checked,
    channel: channel.value,
    key: key.value.trim(),
    webhook_url: webhook.value.trim(),
  });

  const msg = el("p", { class: "hint" });
  const save = el("button", { class: "primary", text: "保存" });
  save.onclick = async () => {
    save.disabled = true;
    try {
      await postJSON("/api/settings/notify", payload());
      toast("已保存");
      msg.textContent = "";
    } catch (err) {
      failure(err);
    }
    save.disabled = false;
  };

  const test = el("button", { class: "ghost", text: "发一条测试推送" });
  test.onclick = async () => {
    test.disabled = true;
    test.textContent = "发送中…";
    try {
      // 先存一版再测：不然"刚填的还没保存，测的还是旧配置"，结果对不上号
      await postJSON("/api/settings/notify", payload());
      const r = await postJSON("/api/settings/notify/test", {});
      msg.textContent = r.ok
        ? "测试推送已发出，去微信里看看。"
        : `没发出去：${r.detail}`;
    } catch (err) {
      failure(err);
    }
    test.disabled = false;
    test.textContent = "发一条测试推送";
  };

  view.replaceChildren(
    el("div", { class: "card flat" },
      el("div", { class: "name", text: "微信推送" }),
      el("p", { class: "hint", text:
        "任务完成、失败、等你批准、撞上绊线时发一条微信。网页里本来就能看到的细节不会推。" }),
      el("div", { class: "fields" },
        el("div", { class: "check-row" }, enabled, el("span", { text: "开启推送" })),
        el("label", {}, "渠道", channel),
        keyRow,
        webhookRow,
      ),
      el("div", { style: "display:flex; gap:8px; margin-top:12px" }, save, test),
      msg,
    ),
    el("p", { class: "hint", text:
      "Server酱：sct.ftqq.com 微信扫码 → 复制 SendKey。PushPlus：pushplus.plus 微信扫码 → 复制 token。" +
      "企业微信：任意群里加「群机器人」→ 复制 webhook 地址。推送发的是事件摘要，点开网页看完整过程。" }),
  );
}

// ----------------------------------------------------------------------
// 视图：监控台
// ----------------------------------------------------------------------

/** 监控台上的任务卡。活跃任务可中断；终态任务只读。点卡片进任务页看实时事件流。 */
function monitorCard(task, projectName, { active, onChanged }) {
  const card = el("div", { class: `card${task.status === "running" ? " running" : ""}` },
    el("div", { class: "name", text: short(task.prompt.replace(/\s+/g, " "), 70) }),
    el("div", { class: "path", text: projectName || task.project_id, title: task.project_id }),
    el("div", { class: "sub" },
      statusPill(task.status),
      el("span", { text: active
        ? (task.started_at
          ? `已跑 ${fmtDuration((Date.now() / 1000 - task.started_at) * 1000)}`
          : `提交于 ${relTime(task.created_at)}`)
        : relTime(task.finished_at || task.created_at) }),
      task.agent_id ? el("span", { text: agentLabel(task.agent_id) }) : null,
      task.cost_usd !== null && task.cost_usd !== undefined ? el("span", { text: fmtCost(task.cost_usd) }) : null,
      !active && task.exit_reason ? el("span", { text: task.exit_reason }) : null,
    ),
  );
  card.onclick = () => go(`#/t/${encodeURIComponent(task.id)}`);

  if (active) {
    const btn = el("button", { class: "danger", text: "中断", style: "margin-top:8px" });
    btn.onclick = async (e) => {
      e.stopPropagation();   // 点的是"中断"，不是"进任务页"
      btn.disabled = true;
      btn.textContent = "中断中…";
      try {
        await postJSON(`/api/tasks/${encodeURIComponent(task.id)}/cancel`, {});
        // 不把 200 当"已经停了"：终态会随下一次刷新（或任务页的 task 帧）到达
        toast("已发出中断请求，等它落地");
        onChanged && onChanged();
      } catch (err) {
        failure(err);
        btn.disabled = false;
        btn.textContent = "中断";
      }
    };
    card.append(btn);
  }
  return card;
}

/** 电脑前的主场：所有任务的运行状态一屏看完，顺手把设备管了。
 *
 * 数据全部来自现成接口（/api/tasks、/api/projects、/api/devices），靠轮询刷新——
 * 库是唯一事实来源，聚合视图没有比"定期去问库"更正确的姿势。
 * 只有"设备"这块是主机专属，判断方法和首页一样：先问 whoami，不拿 403 试探。
 */
async function viewOverview() {
  setTitle("监控台");
  setBack("#/");

  const view = $("#view");
  const me = await api("/api/whoami").catch(() => null);
  const isHost = !!(me && me.host);

  const main = el("div", { class: "col-main" });
  const board = el("div", { class: `console${isHost ? " host" : ""}` }, main);
  view.replaceChildren(board);

  // 设备列表很少变，每 5 秒重画一遍只会让人正要点的按钮突然挪位置——
  // 内容没变就不动它。任务区相反，就是要让它动（耗时在走、状态在变）。
  let lastDevices = "";

  // 在途竞态：导航走的那一刻 teardown 会点亮 dead，已经在路上的这次 fetch
  // 返回后必须把结果（连同 setConn）整个丢掉——不然旧视图会把连接指示灯
  // 和 DOM 一起污染到新视图上。stub 要在第一次 await 之前立好。
  let dead = false;
  let timer = null;
  teardown = () => { dead = true; clearInterval(timer); };

  async function refresh() {
    const [projectsPayload, tasksPayload, devicesPayload] = await Promise.all([
      api("/api/projects"),
      api("/api/tasks?limit=500"),
      isHost ? api("/api/devices") : Promise.resolve(null),
    ]);
    if (dead) return;
    setConn("on", "5s 自动刷新");

    const names = new Map(projectsPayload.projects.map((p) => [p.id, p.name]));
    const tasks = tasksPayload.tasks;
    const active = tasks.filter((t) => !TERMINAL.has(t.status));
    const recent = tasks.filter((t) => TERMINAL.has(t.status)).slice(0, 8);

    main.replaceChildren(
      el("div", { class: "section-title" }, `运行中（${active.length}）`),
      el("p", { class: "hint", text: "每 5 秒自动刷新。点卡片进任务页看实时事件流。" }),
      // 两个 map 必须展开：replaceChildren 不认数组，会把一排卡片转成
      // "[object HTMLDivElement],…" 一行字（完成任务够 8 条时必现）。
      ...(active.length
        ? active.map((t) => monitorCard(t, names.get(t.project_id),
            { active: true, onChanged: () => refresh().catch(() => {}) }))
        : [el("div", { class: "empty", text: "现在没有在跑的任务。去项目页提交一个，这里会自己亮起来。" })]),
      el("div", { class: "section-title", text: `最近完成（${recent.length}）` }),
      ...(recent.length
        ? recent.map((t) => monitorCard(t, names.get(t.project_id), { active: false }))
        : [el("div", { class: "empty", text: "还没有跑完过任务。" })]),
    );

    if (isHost && devicesPayload) {
      const digest = JSON.stringify(devicesPayload);
      if (digest !== lastDevices) {
        lastDevices = digest;
        const side = el("div", { class: "col-side" });
        if (me.public_url) {
          const qrImg = el("img", { src: "/api/qr.svg", alt: "手机扫码打开本系统" });
          const qrBox = el("div", { class: "qr" }, qrImg);
          qrImg.addEventListener("error", () => qrBox.remove());   // 二维码拿不到就整块撤掉，别留白板
          side.append(
            el("div", { class: "section-title", text: "手机连接" }),
            qrBox,
            el("p", { class: "hint", text: "手机扫码直接打开，网址不用手输。" }),
          );
        }
        side.append(
          el("div", { class: "section-title" },
            `设备（${devicesPayload.devices.length}）`,
            el("span", { style: "float:right" },
              el("button", { class: "ghost", text: "配对码", onclick: () => go("#/devices") }))),
        );
        if (devicesPayload.devices.length) {
          for (const d of devicesPayload.devices) {
            side.append(fmtDevice(d, () => refresh().catch(() => {})));
          }
        } else {
          side.append(el("div", { class: "empty", text: "还没有配对过任何设备。" }));
        }
        side.append(el("p", { class: "hint", text: "吊销立刻生效，那台设备要重新配对。" }));
        const existing = board.querySelector(".col-side");
        if (existing) existing.replaceWith(side);
        else board.append(side);
      }
    }
  }

  await refresh();
  if (dead) return;   // 用户在首次加载期间就走了：别装定时器，也别覆盖 teardown
  timer = setInterval(() => {
    if (document.hidden) return;   // 后台标签页别空转，回来那轮自然会刷新
    refresh().catch((err) => {
      // 吊销可能发生在页面开着的时候：下一次轮询就该把人送回配对页
      // （横幅会写明原因），而不是永远停在旧画面上闪"刷新失败"。
      if (err && err.reauth) { teardown(); go("#/pair"); return; }
      setConn("off", "刷新失败，重试中");
    });
  }, 5000);
}

// ----------------------------------------------------------------------
// 视图：项目列表
// ----------------------------------------------------------------------

/** 首页的「新建项目」入口：平时是一条虚线的「＋ 新建项目」，点了展开表单。
 *
 * 扫描只能发现"被 Agent 用过"的目录，全新目录必须手动登记——这是
 * 手机上新开项目的唯一入口，所以要做成列表里第一眼就能看到的卡片，
 * 而不是顶栏里一个不起眼的小按钮（第一版那样，用户根本没看见）。
 *
 * 手机上敲绝对路径太折磨：位置用目录浏览器一路点下去（/api/fs/directories，
 * 从盘符开始，也可以直接改输入框），名称留空 = 就登记这个目录本身。
 */
function buildAddProjectCard() {
  const locationInput = el("input", {
    type: "text",
    placeholder: "项目建在哪？比如 D:\\PythonProjects",
  });
  const nameInput = el("input", {
    type: "text",
    placeholder: "项目目录名，可留空（留空 = 就用上面的目录）",
  });
  const dirBox = el("div", { class: "dir-picker", hidden: true });
  const browseBtn = el("button", { class: "ghost", type: "button", text: "浏览" });
  const addSubmit = el("button", { class: "primary", text: "创建" });

  async function loadDirs(path) {
    dirBox.hidden = false;
    dirBox.replaceChildren(el("div", { class: "empty", text: "读取中…" }));
    try {
      const data = await api(
        `/api/fs/directories?path=${encodeURIComponent(path || "")}`,
      );
      const rows = [
        el("div", { class: "dir-current", text: data.path || "（选一个盘）" }),
      ];
      if (data.parent) {
        const up = el("button", { class: "ghost", type: "button", text: "← 上一级" });
        up.onclick = () => {
          locationInput.value = data.parent;
          loadDirs(data.parent);
        };
        rows.push(up);
      }
      if (!data.directories.length) {
        rows.push(el("div", { class: "hint", text: "下面没有子目录了。" }));
      }
      for (const d of data.directories) {
        const btn = el("button", { class: "ghost dir-row", type: "button", text: d.name });
        btn.onclick = () => {
          locationInput.value = d.path;
          loadDirs(d.path);
        };
        rows.push(btn);
      }
      dirBox.replaceChildren(...rows);
    } catch (err) {
      dirBox.replaceChildren(el("div", { class: "hint", text: failureText(err) }));
    }
  }
  browseBtn.onclick = () => loadDirs(locationInput.value.trim());

  const form = el("form", { class: "new-task", hidden: true },
    el("div", { class: "row" }, locationInput, browseBtn),
    dirBox,
    nameInput,
    el("div", { class: "row" }, addSubmit),
    el("p", { class: "hint", text:
      "名称不存在会自动创建；名称留空就把上面这个目录直接登记成项目。创建后第一个任务会以它为工作目录跑起来。" }),
  );
  form.onsubmit = async (e) => {
    e.preventDefault();
    const loc = locationInput.value.trim();
    if (!loc) {
      toast("先选一个位置：点「浏览」一路点下去，或直接填路径");
      return;
    }
    const name = nameInput.value.trim();
    // 拼接跟随已有的分隔符风格：Windows 路径用 \，POSIX 用 /
    const sep = loc.includes("\\") ? "\\" : "/";
    const path = name
      ? (loc.endsWith(sep) || loc.endsWith("/") ? loc : loc + sep) + name
      : loc;
    addSubmit.disabled = true;
    addSubmit.textContent = "创建中…";
    try {
      const { project } = await postJSON("/api/projects", {
        path, create: !!name,
      });
      go(`#/p/${encodeURIComponent(project.id)}`);
    } catch (err) {
      failure(err);
      addSubmit.disabled = false;
      addSubmit.textContent = "创建";
    }
  };

  const toggle = el("div", { class: "card add-project" }, "＋ 新建项目");
  toggle.onclick = () => {
    form.hidden = !form.hidden;
    if (!form.hidden) loadDirs(locationInput.value.trim());
  };
  return el("div", {}, toggle, form);
}

/** 首启引导卡：只在"主机身份 + 一台活跃设备都没有"时出现，配上第一台手机就消失。
 *
 * 三步各对应一个已有能力（Agent 检测 / 扫码配对 / 微信推送），
 * 这里只负责把顺序讲给人听——不做任何新流程，避免引导和真实入口长成两套。
 * 没就绪的 Agent 也要点名带上原因：否则"为什么只有 Claude Code"这种问题
 * 只能靠猜（多半是 exe 旁边没有 config.toml，OpenCode 的启动命令没配上）。
 */
function buildOnboardingCard(agentsPayload, me) {
  const agents = agentsPayload.agents || [];
  const ready = agents.filter((a) => a.status === "ready");
  const missing = agents.filter((a) => a.status !== "ready");
  const names = ready.map((a) => a.name).join("、") || "还没检测到可用的 Agent——先确认 claude / qoder / opencode / workbuddy 能在电脑上跑";

  const pairBtn = el("button", { class: "primary", text: "手机扫码配对" });
  const pairBox = el("div");
  pairBtn.onclick = () => renderPairQr(pairBox, pairBtn);

  // 扫码地址跟着配置走：配了公网隧道就随处可扫；没配才退回局域网地址（要同一 Wi-Fi）。
  const anywhere = !!(me && me.public_url);
  return el("div", { class: "card" },
    el("div", { class: "name", text: "第一次用？三步上手" }),
    el("div", { class: "path", text: `① 电脑上就绪的 Agent：${names}` }),
    ...missing.map((a) => el("div", { class: "path hint",
      text: `　${a.name} 没就绪：${a.detail || "未检测到"}` })),
    el("div", { class: "path", text: anywhere
      ? "② 手机扫码配对（走公网隧道地址，人在哪都能扫）"
      : "② 手机扫码配对（和电脑连同一个 Wi-Fi；配好公网隧道后随处可扫）" }),
    pairBtn,
    pairBox,
    el("div", { class: "path", text: "③（可选）任务完成时在微信里收到通知" }),
    el("button", { class: "ghost", text: "去设置微信推送", onclick: () => go("#/settings") }),
  );
}

async function viewProjects() {
  setTitle("Keeper");
  setBack(null);

  const view = $("#view");
  view.replaceChildren(el("div", { class: "empty", text: "载入中…" }));

  const [{ projects }, agentsPayload, me] = await Promise.all([
    api("/api/projects"),
    api("/api/agents").catch(() => ({ agents: [] })),
    api("/api/whoami").catch(() => null),
  ]);

  // 设备管理只对主机 token 开放（那是"人在电脑前"才做的事）。
  // 先问清"我是谁"再决定去不去取列表——比"试探 /api/devices 会不会 403"
  // 干净：403 是**拒绝**，不该被当成一种探测手段来用。
  const devicesPayload =
    me && me.host ? await api("/api/devices").catch(() => null) : null;

  const nodes = [];

  // 首启引导：主机身份 + 没有任何活跃设备 = 第一次用。配上一台设备后这张卡就不再出现。
  if (me && me.host && devicesPayload
      && !devicesPayload.devices.some((d) => !d.revoked)) {
    nodes.push(buildOnboardingCard(agentsPayload, me));
  }

  const ready = agentsPayload.agents.filter((a) => a.status === "ready");
  const agentDefaults = agentsPayload.defaults || {};
  primeAgentLabels(agentsPayload);
  if (!ready.length) {
    nodes.push(el("div", { class: "banner", text:
      "没有检测到可用的 Agent。任务可以提交，但会立刻失败——先确认 claude / qoder / opencode / workbuddy 命令能用。" }));
  }

  const refreshBtn = el("button", { class: "ghost", text: "重新扫描" });
  refreshBtn.onclick = async () => {
    refreshBtn.disabled = true;
    refreshBtn.replaceChildren(el("span", { class: "spin" }), " 扫描中…");
    try {
      const data = await postJSON("/api/projects/refresh", {});
      if (data.detail) toast(data.detail);
      await viewProjects();
    } catch (err) {
      failure(err);
      refreshBtn.disabled = false;
      refreshBtn.textContent = "重新扫描";
    }
  };

  const titleRow = el("span", { style: "float:right" });
  const monitorBtn = el("button", { class: "ghost", text: "监控台" });
  monitorBtn.onclick = () => go("#/overview");
  titleRow.append(monitorBtn);

  const settingsBtn = el("button", { class: "ghost", text: "设置" });
  settingsBtn.onclick = () => go("#/settings");
  titleRow.append(settingsBtn);

  if (devicesPayload) {
    const active = devicesPayload.devices.filter((d) => !d.revoked).length;
    const devicesBtn = el("button", { class: "ghost", text: `设备管理（${active}）` });
    devicesBtn.onclick = () => go("#/devices");
    titleRow.append(devicesBtn);
  }
  titleRow.append(refreshBtn);
  nodes.push(el("div", { class: "section-title" },
    `项目（${projects.length}）`, titleRow));

  nodes.push(buildAddProjectCard());

  if (!projects.length) {
    nodes.push(el("div", { class: "empty", text: "还没有项目。\n点上面的「重新扫描」去本机的 Agent 存储里找。" }));
  }

  for (const project of projects) {
    const card = el("div", { class: "card" },
      el("div", { class: "name", text: project.name }),
      el("div", { class: "path", text: project.path, title: project.path }),
      el("div", { class: "sub" },
        project.git_branch ? el("span", { text: `⑂ ${project.git_branch}` }) : null,
        project.session_count ? el("span", { text: `${project.session_count} 个会话` }) : null,
        project.last_active_at ? el("span", { text: relTime(project.last_active_at) }) : null,
      ),
    );
    card.onclick = () => go(`#/p/${encodeURIComponent(project.id)}`);
    nodes.push(card);
  }

  view.replaceChildren(...nodes);
}

// ----------------------------------------------------------------------
// 视图：单个项目
// ----------------------------------------------------------------------

async function viewProject(projectId) {
  const view = $("#view");
  const [data, agentsPayload] = await Promise.all([
    api(`/api/projects/${encodeURIComponent(projectId)}`),
    api("/api/agents").catch(() => ({ agents: [], defaults: {} })),
  ]);
  const { project, sessions } = data;
  const ready = agentsPayload.agents.filter((a) => a.status === "ready");
  const agentDefaults = agentsPayload.defaults || {};
  primeAgentLabels(agentsPayload);

  setTitle(project.name);
  setBack("#/");

  const nodes = [];

  // ---- 新建任务 ----
  const textarea = el("textarea", {
    placeholder: "让 Agent 做什么？",
    rows: 3,
  });
  // 「全部放行」排第一，也就是默认选中——和网关侧的默认一致。
  // 两处默认值不一致是个很隐蔽的坑：手机上看是"默认"，实际发上去的是别的东西。
  const autonomy = el("select", {},
    el("option", { value: "full", text: "全部放行" }),
    el("option", { value: "read_only", text: "只读" }),
    el("option", { value: "edit", text: "可改文件" }),
  );
  // 有多个可用 Agent 才显示选择器：只有一个时它是纯噪音。
  // 默认值来自 /api/agents 的 defaults——在这里写死一份，迟早和网关不一致。
  let agentSel = null;
  if (ready.length > 1) {
    agentSel = el("select", {},
      ...ready.map((a) => el("option", { value: a.id, text: a.name })),
    );
    if (ready.some((a) => a.id === agentDefaults.agent)) agentSel.value = agentDefaults.agent;
  }
  const submit = el("button", { class: "primary", text: "开始任务" });

  let resumeFrom = null;
  const resumeChip = el("div", { class: "chip", hidden: true });

  const form = el("form", { class: "new-task" },
    textarea,
    el("div", { class: "row" }, autonomy, agentSel, submit),
    el("p", { class: "hint", text: "「全部放行」下 Agent 照常工作，只有撞上绊线（递归删除、强推、关机等）才停下来等你。命中会推到这里。" }),
  );

  form.onsubmit = async (e) => {
    e.preventDefault();
    const prompt = textarea.value.trim();
    if (!prompt) return;
    submit.disabled = true;
    submit.textContent = "提交中…";
    try {
      const body = { project_id: project.id, prompt, autonomy: autonomy.value };
      if (agentSel) body.agent_id = agentSel.value;
      if (resumeFrom) body.resume_from = resumeFrom;
      const { task } = await postJSON("/api/tasks", body);
      go(`#/t/${encodeURIComponent(task.id)}`);
    } catch (err) {
      failure(err);
      submit.disabled = false;
      submit.textContent = "开始任务";
    }
  };

  nodes.push(el("div", { class: "section-title", text: "新建任务" }));
  nodes.push(form);
  nodes.push(resumeChip);

  // ---- 历史会话 ----
  nodes.push(el("div", { class: "section-title", text: `历史会话（${sessions.length}）` }));
  if (!sessions.length) {
    nodes.push(el("div", { class: "empty", text: "这个项目还没有历史会话。" }));
  }
  for (const session of sessions) {
    const openable = !!session.native_session_id;
    const card = el("div", { class: `card${openable ? "" : " flat"}` },
      el("div", { class: "name", text: session.title || "（无标题）" }),
      el("div", { class: "sub" },
        el("span", { text: agentLabel(session.agent_id) || "?" }),
        session.git_branch ? el("span", { text: `⑂ ${session.git_branch}` }) : null,
        session.last_active_at ? el("span", { text: relTime(session.last_active_at) }) : null,
      ),
    );
    if (openable) {
      card.onclick = () =>
        go(`#/p/${encodeURIComponent(project.id)}/s/${encodeURIComponent(session.native_session_id)}`);
      const btn = el("button", { text: "续接", style: "margin-top:8px" });
      btn.onclick = (e) => {
        e.stopPropagation();   // 点的是"续接"，不是"进会话"
        resumeFrom = session.native_session_id;
        resumeChip.hidden = false;
        resumeChip.replaceChildren(
          el("span", { text: `续接自：${short(session.title || session.native_session_id, 32)}` }),
          el("button", { text: "×", onclick: () => { resumeFrom = null; resumeChip.hidden = true; } }),
        );
        textarea.focus();
        view.scrollTo({ top: 0, behavior: "smooth" });
      };
      card.append(btn);
    }
    nodes.push(card);
  }

  // ---- 任务历史 ----
  const { tasks } = await api(`/api/tasks?project=${encodeURIComponent(project.id)}`);
  nodes.push(el("div", { class: "section-title", text: `任务（${tasks.length}）` }));
  if (!tasks.length) {
    nodes.push(el("div", { class: "empty", text: "还没有任务。" }));
  }
  for (const task of tasks) {
    const card = el("div", { class: "card" },
      el("div", { class: "name", text: short(task.prompt.replace(/\s+/g, " "), 70) }),
      el("div", { class: "sub" },
        statusPill(task.status),
        task.agent_id ? el("span", { text: agentLabel(task.agent_id) }) : null,
        el("span", { text: relTime(task.created_at) }),
        task.cost_usd !== null && task.cost_usd !== undefined ? el("span", { text: fmtCost(task.cost_usd) }) : null,
        task.exit_reason ? el("span", { text: task.exit_reason }) : null,
      ),
    );
    card.onclick = () => go(`#/t/${encodeURIComponent(task.id)}`);
    nodes.push(card);
  }

  view.replaceChildren(...nodes);
}

// ----------------------------------------------------------------------
// 视图：单个历史会话
// ----------------------------------------------------------------------

/** 一条历史消息 → DOM。工具调用压成一行；用户消息给气泡；助手文本走事件流的排版。 */
function renderHistoryMessage(m) {
  if (m.tool) {
    return metaLine("▸", `工具 ${m.tool}`, m.detail);
  }
  if (m.role === "user") {
    return el("div", { class: "ev user-msg" },
      el("div", { class: "who", text: m.ts ? `你 · ${clockTime(m.ts)}` : "你" }),
      el("div", { class: "body", text: m.text }),
    );
  }
  return textBlock(m.text);
}

/** 「在这个会话里继续」表单：发出去就是一次带 ``resume_from`` 的普通建任务，
 *  Agent 带着会话上下文接着干，提交后跳到新任务页看实时事件流。
 *
 *  会话页和任务页（终态）共用。不把任务页内嵌成"迷你聊天窗"：
 *  实时事件流、断线恢复、中断，那些任务页都有且都验过，复制一份只会改漏。
 */
function resumeForm(projectId, nativeId) {
  const textarea = el("textarea", { placeholder: "接着这个会话说点什么 / 派点什么活？", rows: 3 });
  const autonomy = el("select", {},
    el("option", { value: "full", text: "全部放行" }),
    el("option", { value: "read_only", text: "只读" }),
    el("option", { value: "edit", text: "可改文件" }),
  );
  const submit = el("button", { class: "primary", text: "发送" });

  const form = el("form", { class: "new-task" },
    textarea,
    el("div", { class: "row" }, autonomy, submit),
    el("p", { class: "hint", text: "发出的内容会续接这个会话——Agent 带着上面的上下文继续干活，提交后跳到任务页看实时事件流。" }),
  );
  form.onsubmit = async (e) => {
    e.preventDefault();
    const prompt = textarea.value.trim();
    if (!prompt) return;
    submit.disabled = true;
    submit.textContent = "发送中…";
    try {
      const body = { project_id: projectId, prompt, autonomy: autonomy.value, resume_from: nativeId };
      const { task } = await postJSON("/api/tasks", body);
      go(`#/t/${encodeURIComponent(task.id)}`);
    } catch (err) {
      failure(err);
      submit.disabled = false;
      submit.textContent = "发送";
    }
  };
  return form;
}

/** 会话详情：最近聊了什么 + 直接在这个会话里续发任务。
 *
 * 发送就是一次带 ``resume_from`` 的普通建任务——Agent 会带着整个会话上下文
 * 继续干活，跳到任务页看实时事件流。不在这里内嵌一个"迷你任务页"：
 * 实时事件流、断线恢复、中断，那些任务页都有且都验过，复制一份只会改漏。
 */
async function viewSession(projectId, nativeId) {
  const view = $("#view");
  const [detail, history] = await Promise.all([
    api(`/api/projects/${encodeURIComponent(projectId)}`),
    api(
      `/api/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(nativeId)}/messages?limit=80`,
    ).catch(() => null),   // 读不出历史不该把整页带走——下面照样能续发任务
  ]);
  const { project, sessions } = detail;
  const session = sessions.find((s) => s.native_session_id === nativeId);

  setTitle(session ? short(session.title, 24) : "会话");
  setBack(`#/p/${encodeURIComponent(projectId)}`);

  const nodes = [];

  nodes.push(el("div", { class: "task-head" },
    el("div", { class: "prompt", text: session ? session.title : nativeId }),
    el("div", { class: "meta" },
      el("span", { text: project.name }),
      session && session.git_branch ? el("span", { text: `⑂ ${session.git_branch}` }) : null,
      session && session.last_active_at ? el("span", { text: relTime(session.last_active_at) }) : null,
    ),
  ));

  nodes.push(el("div", { class: "section-title", text: "最近消息" }));
  if (history && history.messages.length) {
    const log = el("div", { class: "stream" });
    for (const m of history.messages) log.append(renderHistoryMessage(m));
    nodes.push(log);
    if (history.messages.length >= 80) {
      nodes.push(el("p", { class: "hint", text: "只显示了最近 80 条，更早的没有拉取。" }));
    }
  } else if (history) {
    nodes.push(el("div", { class: "empty", text: "这个会话没有可显示的消息。" }));
  } else {
    nodes.push(el("div", { class: "empty", text: "历史消息读不出来（这个 Agent 还不支持，或会话文件已被清理）。" }));
  }

  nodes.push(el("div", { class: "section-title", text: "在这个会话里继续" }));
  nodes.push(resumeForm(projectId, nativeId));

  view.replaceChildren(...nodes);
  // 聊天记录要看的是最新的一条，直接落到底部
  view.scrollTop = view.scrollHeight;
}

// ----------------------------------------------------------------------
// 视图：单个任务
// ----------------------------------------------------------------------

async function viewTask(taskId) {
  const view = $("#view");
  const data = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  let task = data.task;

  setTitle("任务");
  setBack(task.project_id ? `#/p/${encodeURIComponent(task.project_id)}` : "#/");

  const promptEl = el("div", { class: "prompt", text: task.prompt });
  const pillSlot = el("span", {}, statusPill(task.status));
  const metaEl = el("span");
  const cancelBtn = el("button", { class: "danger", text: "中断" });

  const head = el("div", { class: "task-head" },
    promptEl,
    el("div", { class: "meta" }, pillSlot, metaEl, el("span", { class: "spacer" }), cancelBtn),
  );

  const stream = el("div", { class: "stream" });
  // 终态后在这里亮出"续接"表单——对话就是任务页的接力，不用绕回会话页再进来
  const followSlot = el("div");

  function paintTask(t) {
    task = t;
    pillSlot.replaceChildren(statusPill(t.status));
    metaEl.textContent = [
      agentLabel(t.agent_id),
      clockTime(t.started_at || t.created_at),
      t.finished_at && t.started_at ? fmtDuration((t.finished_at - t.started_at) * 1000) : "",
      fmtCost(t.cost_usd),
      t.exit_reason || "",
    ].filter(Boolean).join(" · ");
    const done = TERMINAL.has(t.status);
    cancelBtn.disabled = done;
    cancelBtn.textContent = done ? "已结束" : "中断";
    // 会话 id 是续接的接力棒（断连任务的恢复也走这条路）。终态只进不退，装一次就够
    if (done && t.native_session_id && !followSlot.childNodes.length) {
      followSlot.append(
        el("div", { class: "section-title", text: "在这个会话里继续" }),
        resumeForm(t.project_id, t.native_session_id),
      );
    }
  }

  cancelBtn.onclick = async () => {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "中断中…";
    try {
      await postJSON(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, {});
      // 不把 200 当作"已经停了"：真正的停止会以 task 帧和事件流里的终态到达
      toast("已发出中断请求，等它落地");
    } catch (err) {
      failure(err);
    } finally {
      paintTask(task);
    }
  };

  paintTask(task);
  view.replaceChildren(head, el("div", { class: "section-title", text: "事件流" }), stream, followSlot);

  const toolCards = new Map();
  const permCards = new Map();

  function append(node, alreadyInDom = false) {
    if (!alreadyInDom) stream.append(node);
    if (stick) view.scrollTop = view.scrollHeight;
  }

  // since=0：页面每次打开都从库里完整补发。这就是"关掉再打开事件不丢"，
  // 前端不需要为此存任何东西。
  teardown = openStream(taskId, {
    since: 0,
    onEvent: (event) => {
      const node = renderEvent(event, toolCards, append, permCards);
      if (node) append(node);
    },
    onTask: paintTask,
    onConn: setConn,
    onFatal: (message) => { setConn("bad", "出错"); toast(message); },
  }).close;
}

// ----------------------------------------------------------------------
// 启动：先把地址栏里的 ?token= 换成 cookie，再进路由
// ----------------------------------------------------------------------

/** 把 `?token=` 换成一份 cookie，然后把它从地址栏抹掉。
 *
 * **不能只靠页面响应去种这个 cookie。** 页面文档是浏览器最爱缓存的东西：
 * 实测同一个 `/?token=…` 第二次打开时，浏览器直接从缓存取文档、
 * 服务端连请求都没收到，于是 Set-Cookie 根本没被重新执行
 * （连 `Cache-Control: no-store` 也没拦住）。而用户干这件事的方式恰恰是
 * "点书签"/"从历史记录里再点一次"——恰好就是缓存命中的那条路。
 *
 * 所以改成主动打一次接口来换。这条响应是 XHR、没有可缓存的验证器，
 * 而且中间件给所有"种了凭证的响应"都加了 no-store，稳。
 *
 * 换完把 token 从地址栏抹掉：它本来就是一次性的递送手段，
 * 留在地址栏只会进历史记录、被截图、被顺手复制走。
 */
async function adoptUrlToken() {
  const params = new URLSearchParams(location.search);
  const token = params.get("token");
  if (!token) return;

  try {
    await api(`/api/whoami?token=${encodeURIComponent(token)}`);
  } catch {
    // token 不对或过期了。不用在这里报错——下面正常路由时，
    // 第一次 API 调用就会 401，界面自然会把人带到配对页。
  }

  params.delete("token");
  const rest = params.toString();
  history.replaceState(
    null, "",
    location.pathname + (rest ? `?${rest}` : "") + location.hash,
  );
}

window.addEventListener("hashchange", route);
adoptUrlToken().then(() => {
  // 后台预取 Agent 展示名：detect 要起子进程，不能挡首屏，兜底映射顶第一屏
  api("/api/agents").then(primeAgentLabels).catch(() => {});
  route();
});
