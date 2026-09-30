/* Remote Agent Workspace — 第 5 层前端（原生 ES 模块，无构建步骤）
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
function renderEvent(event, toolCards, append) {
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
    el("div", { class: "pair-intro" , text:
      "网关那台电脑的启动横幅上有一个 6 位配对码。把它输进来，这台设备就拿到了自己的凭证——" +
      "以后不用再输，也不会在网址里带着长期凭证。" }),
    form,
    message,
    el("p", { class: "hint", text: "配对码 15 分钟内有效，而且只能配一台设备。" }),
  );

  code.focus();
}

// ----------------------------------------------------------------------
// 视图：设备管理
// ----------------------------------------------------------------------

function fmtDevice(d) {
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
        await viewDevices();
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

async function viewDevices() {
  setTitle("设备");
  setBack("#/");

  const view = $("#view");
  const data = await api("/api/devices");

  const nodes = [];
  const pairing = data.pairing || {};
  nodes.push(el("div", { class: "banner", text: pairing.active
    ? `当前配对码还有 ${Math.round((pairing.seconds_left || 0) / 60)} 分钟有效。看不到它就回网关那台机器上看横幅。`
    : "当前没有有效的配对码。在网关那台机器上跑 `python -m gateway code` 换一个。" }));

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
// 视图：项目列表
// ----------------------------------------------------------------------

async function viewProjects() {
  setTitle("Remote Agent Workspace");
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

  const ready = agentsPayload.agents.filter((a) => a.status === "ready");
  if (!ready.length) {
    nodes.push(el("div", { class: "banner", text:
      "没有检测到可用的 Agent。任务可以提交，但会立刻失败——先确认 claude 命令能用。" }));
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
  if (devicesPayload) {
    const active = devicesPayload.devices.filter((d) => !d.revoked).length;
    const devicesBtn = el("button", { class: "ghost", text: `设备（${active}）` });
    devicesBtn.onclick = () => go("#/devices");
    titleRow.append(devicesBtn);
  }
  titleRow.append(refreshBtn);
  nodes.push(el("div", { class: "section-title" },
    `项目（${projects.length}）`, titleRow));

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
  const data = await api(`/api/projects/${encodeURIComponent(projectId)}`);
  const { project, sessions } = data;

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
  const submit = el("button", { class: "primary", text: "开始任务" });

  let resumeFrom = null;
  const resumeChip = el("div", { class: "chip", hidden: true });

  const form = el("form", { class: "new-task" },
    textarea,
    el("div", { class: "row" }, autonomy, submit),
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
    const card = el("div", { class: "card flat" },
      el("div", { class: "name", text: session.title || "（无标题）" }),
      el("div", { class: "sub" },
        el("span", { text: session.agent_id || "?" }),
        session.git_branch ? el("span", { text: `⑂ ${session.git_branch}` }) : null,
        session.last_active_at ? el("span", { text: relTime(session.last_active_at) }) : null,
      ),
    );
    if (session.native_session_id) {
      const btn = el("button", { text: "续接", style: "margin-top:8px" });
      btn.onclick = () => {
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

  function paintTask(t) {
    task = t;
    pillSlot.replaceChildren(statusPill(t.status));
    metaEl.textContent = [
      clockTime(t.started_at || t.created_at),
      t.finished_at && t.started_at ? fmtDuration((t.finished_at - t.started_at) * 1000) : "",
      fmtCost(t.cost_usd),
      t.exit_reason || "",
    ].filter(Boolean).join(" · ");
    const done = TERMINAL.has(t.status);
    cancelBtn.disabled = done;
    cancelBtn.textContent = done ? "已结束" : "中断";
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
  view.replaceChildren(head, el("div", { class: "section-title", text: "事件流" }), stream);

  const toolCards = new Map();

  function append(node, alreadyInDom = false) {
    if (!alreadyInDom) stream.append(node);
    if (stick) view.scrollTop = view.scrollHeight;
  }

  // since=0：页面每次打开都从库里完整补发。这就是"关掉再打开事件不丢"，
  // 前端不需要为此存任何东西。
  teardown = openStream(taskId, {
    since: 0,
    onEvent: (event) => {
      const node = renderEvent(event, toolCards, append);
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
adoptUrlToken().then(route);
