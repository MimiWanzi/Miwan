/* 前端探活逻辑的**确定性**验收：把 web/app.js 里的 openStream 抠出来，
 * 配一套假的 WebSocket、假的定时器、假的 document 跑一遍。
 *
 * 为什么要有这个文件，而不是全靠 verify_web.py：那边是真的浏览器，可信度最高，
 * 但一轮六分钟、还要花三次真实 API 调用，而且**验的是概率**——它只能等到
 * "三十秒里没出事"，等不出"第四十秒会不会出事"。而探活这段代码的关键性质
 * 恰好是时序的：
 *
 *   - 空闲的健康连接**不能**被误判成断了（误判的代价是每个闲着的任务页
 *     半分钟重连一次）
 *   - 静默丢包**必须**被发现（发现不了的代价是用户盯着静止的屏幕以为任务卡了）
 *
 * 这两条在真实浏览器里各要等半分钟，在这里是零耗时、可重复、可以任意构造
 * 边界（正好卡在阈值上、pong 只回来一半、旧连接的回调晚到……）。
 *
 * 抠函数用的是文本提取（找 `function openStream(` 然后配平花括号）。
 * 这确实脆——但脆的方向是**报错**（"找不到 openStream"），不是悄悄测了
 * 一个别的东西。有这一层在，verify_web.py 就只需要负责证明"这套逻辑真的
 * 接在页面上、真的和网关对得上"。
 *
 * 跑法：
 *     node probes/verify_liveness.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_JS = join(HERE, "..", "web", "app.js");

// ----------------------------------------------------------------------
// 假时钟：时间由测试推着走，不等真实时间
// ----------------------------------------------------------------------

let now = 0;
let nextTimerId = 1;
const timers = new Map(); // id -> { at, fn, every }

function schedule(fn, delay, every) {
  const id = nextTimerId++;
  timers.set(id, { at: now + delay, fn, every });
  return id;
}

/** 重新排一个周期定时器。
 *
 * **必须取整**：PING_MS/3 是 3333.333…，一路累加下去第 3 次就落在
 * 9999.999999999998，比阈值小一丁点，于是"该发的 ping"不发，
 * 检查变成偶尔过偶尔不过的噪声。真的 setInterval 没有这种漂移。 */
function reschedule(t) {
  t.at = Math.round(t.at + t.every);
}

function clear(id) {
  timers.delete(id);
}

/** 把时间推 `ms` 毫秒，沿途按时刻顺序把到点的回调都跑掉。 */
function advance(ms) {
  const target = now + ms;
  for (;;) {
    let due = null;
    let dueId = null;
    for (const [id, t] of timers) {
      if (t.at <= target && (due === null || t.at < due.at)) {
        due = t;
        dueId = id;
      }
    }
    if (due === null) break;
    now = due.at;
    if (due.every) reschedule(due);
    else timers.delete(dueId);
    due.fn();
  }
  now = target;
}

// ----------------------------------------------------------------------
// 假环境。openStream 用到的全局量只有这几个，全部在这里顶掉。
// ----------------------------------------------------------------------

const sockets = []; // 按创建顺序记下每一条连接

class WebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  constructor(url) {
    this.url = url;
    this.readyState = WebSocket.CONNECTING;
    this.sent = [];
    this.tried = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    sockets.push(this);
  }

  send(data) {
    // 尝试次数和成功次数分开记：**"关了之后还在试图发"是要验的东西**，
    // 而那种 send 会抛异常（app.js 里被 try 吞掉），只看成功数会漏掉。
    this.tried.push(data);
    if (this.readyState !== WebSocket.OPEN) throw new Error("socket 没开就 send");
    this.sent.push(data);
  }

  /** 前端自己调的 close()。**不触发 onclose**——这是刻意的：
   *  黑洞掉的连接上，浏览器调完 close() 也要等 TCP 自己超时才报错，
   *  而 abandon() 的全部意义就是不依赖那个 onclose。 */
  close() {
    this.readyState = WebSocket.CLOSED;
  }

  // --- 下面是测试用的"对面" ---

  /** 握手成功。 */
  open() {
    this.readyState = WebSocket.OPEN;
    this.onopen?.();
  }

  /** 收到一帧。 */
  recv(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }

  /** 连接断了（RST / 对端关闭），会触发 onclose。 */
  die(code = 1006) {
    if (this.readyState === WebSocket.CLOSED) return;
    this.readyState = WebSocket.CLOSED;
    this.onclose?.({ code });
  }

  get pings() {
    return this.sent.filter((s) => JSON.parse(s).type === "ping").length;
  }

  get pingAttempts() {
    return this.tried.filter((s) => JSON.parse(s).type === "ping").length;
  }
}

const document = { hidden: false };
const location = { protocol: "http:", host: "127.0.0.1:8799" };
const Date = { now: () => now }; // 顶掉真的 Date，时间只从假时钟来

const setTimeout = (fn, delay) => schedule(fn, delay, false);
const setInterval = (fn, delay) => schedule(fn, delay, true);
const clearTimeout = clear;
const clearInterval = clear;

// ----------------------------------------------------------------------
// 抠出 openStream
// ----------------------------------------------------------------------

/** 从 `function openStream(` 起配平花括号。跳过字符串和行注释里的括号。
 *
 * 参数表里有解构（`{ since = 0, ... }`）也算花括号，所以**函数体要从
 * 括号深度为 0 的那个 `{` 才开始算**——否则函数会在参数表结尾就"配平"，
 * 抠出来的是半截。 */
function extractFunction(src, header) {
  const start = src.indexOf(header);
  if (start < 0) throw new Error(`app.js 里找不到 ${header}——前端结构变了，这个探针要跟着改`);

  let parens = 0;
  let depth = 0;
  let started = false;
  let quote = null;

  for (let i = start; i < src.length; i++) {
    const c = src[i];
    if (quote) {
      if (c === "\\") i++;
      else if (c === quote) quote = null;
      continue;
    }
    if (c === "/" && src[i + 1] === "/") {
      i = src.indexOf("\n", i);
      if (i < 0) break;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      quote = c;
      continue;
    }
    if (c === "(") parens++;
    else if (c === ")") parens--;
    else if (c === "{") {
      if (!started) {
        if (parens !== 0) continue; // 参数表里的解构，不是函数体
        started = true;
      }
      depth++;
    } else if (c === "}" && started) {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`${header} 的花括号没配平——提取失败了`);
}

const appSrc = readFileSync(APP_JS, "utf8");
const fnSrc = extractFunction(appSrc, "function openStream(");
// 直接 eval 一个函数表达式：它会闭包在上面的假环境里，看见假 WebSocket / 假时钟
const openStream = eval(`(${fnSrc})`);

// ----------------------------------------------------------------------
// 断言
// ----------------------------------------------------------------------

const results = [];
function check(label, ok, detail = "") {
  results.push({ label, ok: !!ok, detail });
  console.log(`${ok ? "✓" : "✗"} ${label}${detail ? "  " + detail : ""}`);
}

/** 开一条订阅，把回调都记下来。 */
function subscribe(since = 0) {
  const log = { conn: [], events: [], tasks: [], fatal: [] };
  const handle = openStream("t-abc", {
    since,
    onEvent: (e) => log.events.push(e.seq),
    onTask: (t) => log.tasks.push(t.status),
    onConn: (s, why) => log.conn.push(s),
    onFatal: (e) => log.fatal.push(e),
  });
  const sock = sockets[sockets.length - 1];
  return { handle, log, sock, last: () => sockets[sockets.length - 1] };
}

/** 让某条连接按"服务端"的样子开工：握手 + 发 hello。 */
function bringUp(sock, seq = 0, status = "running") {
  sock.open();
  sock.recv({ type: "hello", task: { id: "t-abc", status }, since: seq });
}

/** 往前走 `ms`，但**替服务端回 pong**——模拟一条真实存在的连接。
 *
 * 分小步走是必须的：ping 是异步发出去的，一次 advance 到底的话
 * 中途发出的 ping 都没人回，测出来的就不是"连接好好的"而是"连接死了"。 */
function advanceAlive(sock, ms, step = 500) {
  for (let elapsed = 0; elapsed < ms; elapsed += step) {
    const before = sock.pings;
    advance(Math.min(step, ms - elapsed));
    if (sock.pings > before) sock.recv({ type: "pong" });
  }
}

// ----------------------------------------------------------------------

console.log("前端探活（openStream）——不碰浏览器、不碰网关、零 API 调用\n");

// ---- 1. 起手：连上、hello、状态是 on ----
{
  const { handle, log, sock } = subscribe();
  check("起步就订阅了正确的任务", sock.url.includes("task_id=t-abc"), sock.url);
  check("首次订阅带 since=0（从头补发）", sock.url.includes("since=0"), sock.url);
  bringUp(sock);
  check("hello 之后状态变成已连接", log.conn.at(-1) === "on", JSON.stringify(log.conn));
  check("hello 里的任务状态会被画出来", log.tasks.at(-1) === "running", JSON.stringify(log.tasks));
  handle.close();
}

// ---- 2. 空闲的连接：定时 ping，pong 一回来就不判死 ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe();
  bringUp(sock);
  log.conn.length = 0;

  advance(9_000);
  check("不到 PING_MS 不发 ping（别没事找事）", sock.pings === 0, `已发 ${sock.pings} 个`);

  advanceAlive(sock, 3_000); // 越过 10 秒
  check("过了 PING_MS 主动问一次", sock.pings === 1, `已发 ${sock.pings} 个`);

  // 再空闲一分钟，一路上 pong 都正常回来。**空闲连接上 pong 是唯一会到的帧**：
  // 任务早跑完了，事件一帧都没有，服务端的协议层 ping 浏览器又看不见。
  advanceAlive(sock, 60_000);
  check("一分钟多的空闲之后连接状态一次都没变过", log.conn.length === 0, JSON.stringify(log.conn));
  check(
    "空闲期间只按 PING_MS 的节拍问（约 7 次），没有追问",
    sock.pings >= 6 && sock.pings <= 8,
    `已发 ${sock.pings} 个`,
  );
  check("空闲期间没有另起连接", sock === sockets.at(-1), `共 ${sockets.length} 条`);
  handle.close();
}

// ---- 3. 任务一直在产事件时不必 ping ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe();
  bringUp(sock);
  for (let i = 0; i < 10; i++) {
    advance(5_000);
    sock.recv({ type: "event", event: { seq: i + 1, type: "assistant_text", payload: {} } });
  }
  check(
    "事件流一直在动的时候一个 ping 都不发（lastRx 一直新鲜）",
    sock.pings === 0,
    `50 秒里发了 ${sock.pings} 个`,
  );
  check("事件按 seq 收下了", log.events.length === 10, log.events.join(","));
  check("这期间也没重连", log.conn.filter((c) => c === "off").length === 0);
  handle.close();
}

// ---- 4. 黑洞：ping 没回音，到点自己判死并换一条 ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe();
  bringUp(sock);
  sock.recv({ type: "event", event: { seq: 7, type: "assistant_text", payload: {} } });
  log.conn.length = 0;
  const t0 = now;

  // 从这里开始对面静默：发什么都行，一帧都不回来。
  // 注意**没有** die()：连接不断，只是不通。这正是真实的那种坏法。
  advance(29_000);
  check("还没到 STALE_MS 就还认为连着", log.conn.length === 0, `${now - t0}ms 时 ${JSON.stringify(log.conn)}`);

  advance(8_000); // 越过 30s 阈值（下一次 tick 就动手）
  check("超过 STALE_MS 后自己判死（没有 onclose 可等）", log.conn.includes("off"), JSON.stringify(log.conn));
  check(
    "这次判死是有节制的：3 个 ping 没回音，不是每个 tick 都乱发",
    sock.pings === 3,
    `发了 ${sock.pings} 个`,
  );

  advance(600); // 退避第一档 500ms
  check("判死后另起了一条连接", sockets.length === 2, `共 ${sockets.length} 条`);
  check(
    "重连带着游标（since=最后收到的 seq），不是从头再要一遍",
    sockets.at(-1).url.includes("since=7"),
    sockets.at(-1).url,
  );
  handle.close();
}

// ---- 5. 被判死的那条连接，回调晚到也不能捣乱 ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe({ since: 3 });
  bringUp(sock);
  log.conn.length = 0;
  advance(35_000); // 判死 + 已排了一条新连接
  const afterAbandon = sockets.length;
  const old = sock;

  // 真实的黑洞连接过几分钟才等到 TCP 超时、这时才报错。它**不能**把
  // 重连再排一次（那会让手机在电梯口叠出一堆连接）。
  old.die(1006);
  advance(5_000);
  check(
    "旧连接迟到的 onclose 不会再排出第二条重连",
    sockets.length === afterAbandon,
    `旧连接报错前后都是 ${afterAbandon} 条`,
  );
  check("状态日志没有被这次迟到回调污染", log.conn.filter((c) => c === "off").length === 1, JSON.stringify(log.conn));

  // 而新连接正常工作
  bringUp(sockets.at(-1), 3);
  check("新连接接上之后状态回到 on", log.conn.at(-1) === "on", JSON.stringify(log.conn));
  handle.close();
}

// ---- 6. 握手卡住：TCP 连上了但 upgrade 回不来 ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe();
  bringUp(sock);
  log.conn.length = 0;

  // 网络半死：新连接建得起来（TCP 层成功），但握手响应永远不回。
  // 这种连接**没有 onclose**，探活定时器又要 onopen 之后才启动——
  // 不给它单独一个期限就会永远卡在"重连中"。
  sock.die(1006);
  advance(600); // 退避后新连接建出来了，但它不会 open
  const stuck = sockets.at(-1);
  check("半死的连接确实卡在 CONNECTING（这个前提不成立就测不到东西）",
    stuck.readyState === WebSocket.CONNECTING, `readyState=${stuck.readyState}`);

  advance(31_000);
  check(
    "握手超时后也按死了处理，继续重试",
    sockets.length >= 3,
    `共起了 ${sockets.length} 条连接`,
  );
  handle.close();
}

// ---- 7. 明确断开 vs 静默丢包：两条路都要走通 ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe();
  bringUp(sock);
  log.conn.length = 0;
  sock.die(1006); // 服务端进程退了 / 被 RST
  check("连接断掉立刻就知道（不用等探活）", log.conn.includes("off"), JSON.stringify(log.conn));
  advance(600);
  check("然后立刻重连", sockets.length === 2, `共 ${sockets.length} 条`);
  bringUp(sockets.at(-1));
  check("接上之后状态回到 on", log.conn.at(-1) === "on", JSON.stringify(log.conn));
  handle.close();
}

// ---- 8. 页面切后台：节流不能变成误判，回来之后要主动换一条 ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe();
  bringUp(sock);
  log.conn.length = 0;

  // 锁屏五分钟。浏览器会把定时器节流甚至冻住——"很久没收到消息"
  // 在这里完全是正常的，按它判死就是误杀（每个锁屏都要多一次重连）。
  document.hidden = true;
  advance(300_000);
  check(
    "页面在后台时不因为'太久没消息'判死",
    log.conn.length === 0 && sockets.length === 1,
    `状态 ${JSON.stringify(log.conn)}，连接数 ${sockets.length}`,
  );
  check("后台期间也没往外发 ping（省电，也免得被节流后攒一批）", sock.pings === 0, `发了 ${sock.pings} 个`);

  // 回到前台。这时候那条连接多半真的死了（手机锁屏时系统会收走 socket），
  // 所以判定是**换一条**，而不是"当作没事继续用"。第一次检查就会动手。
  document.hidden = false;
  advance(4_000);
  check(
    "回到前台后主动换一条（不是当作没事继续用）",
    log.conn.includes("off"),
    JSON.stringify(log.conn),
  );
  check("并且真的换出去了", sockets.length === 2, `共 ${sockets.length} 条`);

  const fresh = sockets.at(-1);
  bringUp(fresh);
  advanceAlive(fresh, 40_000);
  check(
    "换过之后照常靠 pong 活着（只换了这一次）",
    log.conn.at(-1) === "on" && log.conn.filter((c) => c === "off").length === 1,
    JSON.stringify(log.conn),
  );
  handle.close();
}

// ---- 9. close() 之后彻底安静 ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe();
  bringUp(sock);
  advance(11_000);
  const triedBefore = sock.pingAttempts;
  handle.close();
  sock.die(1006); // 服务端那边随后也发现对面走了

  advance(120_000);
  // 看的是**尝试**次数：关掉之后如果定时器还在跑，send 会抛异常被 try 吞掉，
  // 只看成功次数是发现不了的。
  check(
    "关掉之后再也没试过发 ping",
    sock.pingAttempts === triedBefore,
    `${triedBefore} → ${sock.pingAttempts}`,
  );
  check("关掉之后不再重连", sockets.length === 1, `共 ${sockets.length} 条连接`);
  check("关掉之后状态不再变化", log.conn.filter((c) => c === "off").length === 0, JSON.stringify(log.conn));
}

// ---- 10. 服务端说"没有这个任务"：不能再重连 ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe();
  bringUp(sock);
  sock.recv({ type: "error", error: "没有这个任务：t-abc" });
  advance(120_000);
  check("服务端明确报错后不再重连（否则是无意义的死循环）", sockets.length === 1, `共 ${sockets.length} 条`);
  check("错误被交给页面去显示", log.fatal.length === 1, JSON.stringify(log.fatal));
  handle.close();
}

// ---- 11. 去重：重连补发与实时推送重叠时按 seq 吃掉 ----
{
  sockets.length = 0;
  const { handle, log, sock } = subscribe();
  bringUp(sock);
  sock.recv({ type: "event", event: { seq: 5, type: "assistant_text", payload: {} } });
  sock.recv({ type: "event", event: { seq: 6, type: "assistant_text", payload: {} } });
  advance(35_000); // 判死重连，since 应该是 6
  bringUp(sockets.at(-1), 6);
  // 服务端从 6 之后补发，但重连和实时可能重叠，6 又送了一遍
  sockets.at(-1).recv({ type: "event", event: { seq: 6, type: "assistant_text", payload: {} } });
  sockets.at(-1).recv({ type: "event", event: { seq: 7, type: "assistant_text", payload: {} } });
  check("重连后重复送来的那一条被吃掉了", log.events.join(",") === "5,6,7", log.events.join(","));
  handle.close();
}

// ----------------------------------------------------------------------

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(64));
if (failed.length) {
  console.log(`  ${results.length - failed.length}/${results.length} 项通过，失败 ${failed.length} 项：`);
  for (const f of failed) console.log(`    ✗ ${f.label}  ${f.detail}`);
} else {
  console.log(`  ${results.length}/${results.length} 项通过`);
}
console.log("=".repeat(64));
process.exit(failed.length ? 1 : 0);
