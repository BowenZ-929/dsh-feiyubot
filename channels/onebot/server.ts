// OneBot v11 反向 WS 通道：我们开 127.0.0.1:6200，协议端（"会说 OneBot 的进程"）连进 /ws。
// M1（默认）：收到消息只记日志——原始事件 + parseEvent 解析结果，不发任何东西。
// M2（加 --core <核地址>）：消息转给核，把核回的 chunks 按 send_at 分时发回来源会话；
// 但「能发」不等于「会发」：QQAGENT_CHANNEL_MODE=silent（默认）时门是关的，只记账、不出站。
// 解析不在这里重写：一律走 M0 已验收的 parse.ts。
import WebSocket, { WebSocketServer } from "ws";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseEvent, type ParsedEvent } from "./parse.ts";
import { LEDGER_RELATIVE_PATH, Ledger, dedupKey, dedupNote, needsErrorRow, readMode, sceneOf, targetOf, type Scene } from "./gate.ts";
import { ACK_TIMEOUT_MS, readCoreTimeout, settleRound, type AckOutcome, type CoreAttempt } from "./outbox.ts";

const HOST = "127.0.0.1";
const PORT = 6200;
const WS_PATH = "/ws";
const LEDGER_PATH = join(dirname(fileURLToPath(import.meta.url)), LEDGER_RELATIVE_PATH);

const now = (): string => new Date().toISOString();
const log = (tag: string, message: string): void => {
  console.log(`${now()} ${tag.padEnd(6)} ${message}`);
};

// 参数各自独立解析：--core-path 写在 --core 前后都算数。
const argOf = (flag: string, fallback: string): string => {
  const index = process.argv.indexOf(flag);
  const value = index >= 0 ? process.argv[index + 1] : undefined;
  return value && !value.startsWith("--") ? value : fallback;
};

// 不给 --core 就是纯 M1；给了才连核。
const CORE_URL = argOf("--core", "");
// 默认 /chat：核自己的聊天入口。要退回 README 里那个旧回声端点，写 --core-path /inbound。
const CORE_PATH = argOf("--core-path", "/chat");
// 只有真给了 --core-timeout 才解析：没给是常态，不该刷 warn；给了却不合法必须说出来。
const CORE_TIMEOUT = readCoreTimeout(
  process.argv.includes("--core-timeout") ? argOf("--core-timeout", "") : null,
);

const describeWho = (event: ParsedEvent): string => {
  const parts = [String(event.who.id)];
  if (event.who.card) parts.push(`名片=${event.who.card}`);
  if (event.who.nickname) parts.push(`昵称=${event.who.nickname}`);
  return parts.join(" ");
};

const describeWhere = (event: ParsedEvent): string =>
  event.where.scene === "group" ? `群 ${event.where.groupId}` : `私聊 ${event.where.userId}`;

// 门在启动时定死：环境变量只读一次，运行中改它不该忽然开口。
const gate = readMode(process.env);
if (gate.warn) log("warn", gate.warn);
if (CORE_TIMEOUT.warn) log("warn", CORE_TIMEOUT.warn);
log("gate", gate.mode === "send" ? "mode=send（会真发出去）" : "mode=silent（想发但被门挡住）");

// 账是全进程一份：去重集合在启动时就把历史读进来，重连和重启都不重发。
const ledger = new Ledger({ path: LEDGER_PATH, onWarn: (message) => log("warn", message) });

let connectionSeq = 0;
let eventSeq = 0;
let requestSeq = 0;

type AckFrame = { echo: string; status: string | null; retcode: number | null; note: string };
type AckWaiter = (outcome: AckOutcome) => void;
const ackWaiters = new Map<string, AckWaiter>();

// 协议端对我们下发 action 的应答：{status, retcode, data, echo}，没有 post_type。
// 判定沿用 M1 的思路；但原始错误码和说明必须摊到日志里——吞掉 retcode 等于把排障线索扔掉。
const isActionAck = (raw: string): AckFrame | null => {
  let frame: unknown;
  try {
    frame = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof frame !== "object" || frame === null) return null;
  const record = frame as Record<string, unknown>;
  if ("post_type" in record) return null;
  if (!("echo" in record) || !("retcode" in record)) return null;
  const data =
    typeof record.data === "object" && record.data !== null && !Array.isArray(record.data)
      ? (record.data as Record<string, unknown>)
      : {};
  const extras: string[] = [];
  const explanations: Array<[string, unknown]> = [
    ["data.message", data.message],
    ["data.wording", data.wording],
    ["message", record.message],
  ];
  for (const [label, value] of explanations) {
    if (typeof value === "string" && value.length > 0) extras.push(`${label}=${JSON.stringify(value)}`);
  }
  const status = typeof record.status === "string" ? record.status : null;
  const retcode = typeof record.retcode === "number" ? record.retcode : null;
  log(
    "ack",
    `收到下发应答 echo=${JSON.stringify(record.echo)} status=${String(record.status)} retcode=${String(record.retcode)}` +
      (extras.length > 0 ? ` ${extras.join(" ")}` : ""),
  );
  return { echo: String(record.echo), status, retcode, note: extras.join(" ") };
};

// 等 ack 必须有超时兜底：协议端不回话时不能把整批吊死，更不能自动重发（可能已经发出去了）。
const waitAck = (echo: string): Promise<AckOutcome> =>
  new Promise((resolve) => {
    const timer = setTimeout(() => {
      ackWaiters.delete(echo);
      resolve({ kind: "timeout" });
    }, ACK_TIMEOUT_MS);
    ackWaiters.set(echo, (outcome) => {
      clearTimeout(timer);
      resolve(outcome);
    });
  });

type ClaimedContext = { id: number; key: string; scene: Scene; target: string; elapsed: () => string };

// 认领之后的一次完整处理：这里只管把这一条走完，不再关心去重（认领已经做过）。
const processClaimed = async (event: ParsedEvent, socket: WebSocket, ctx: ClaimedContext): Promise<void> => {
  const { id, key, scene, target, elapsed } = ctx;

  log(`req#${id}`, `① 收到 ${describeWho(event)} @ ${describeWhere(event)} text=${JSON.stringify(event.text)}`);
  log(`req#${id}`, `② 交给核 POST ${CORE_URL}${CORE_PATH}`);

  // 成功与失败都收成一个 CoreAttempt：后面 settleRound 只按这个值分派，不再关心 fetch 的细节。
  let attempt: CoreAttempt;
  try {
    const response = await fetch(`${CORE_URL}${CORE_PATH}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        who: event.who,
        where: event.where,
        scene,
        text: event.text,
        hasImage: event.hasImage,
        messageId: event.messageId,
      }),
      signal: AbortSignal.timeout(CORE_TIMEOUT.ms),
    });
    const body = await response.text();
    if (!response.ok) {
      attempt = { kind: "http", status: response.status, body };
    } else {
      try {
        attempt = { kind: "payload", payload: JSON.parse(body) };
      } catch (error) {
        attempt = { kind: "unreadable", reason: `不是合法 JSON：${(error as Error).message}` };
      }
    }
  } catch (error) {
    // 核不可达只记一行：崩掉或重试都会把通道变成核的从属进程。
    // 但「等超时了」和「连不上」要分开记：混成一个前缀，排障时就不知道该调超时还是查网络。
    const failure = error as Error;
    const timedOut = failure.name === "TimeoutError" || failure.name === "AbortError";
    attempt = timedOut
      ? { kind: "timeout", timeoutMs: CORE_TIMEOUT.ms, message: failure.message }
      : { kind: "unreachable", message: failure.message };
  }

  await settleRound({
    mode: gate.mode,
    batch: { key, messageId: event.messageId, scene, target },
    where: event.where,
    scene,
    attempt,
    origin: performance.now(),
    requestSeq: id,
    elapsed,
    ledger: { appendNew: (entry) => ledger.appendNew(entry) },
    isGateOpen: () => gate.mode === "send",
    isSocketOpen: () => socket.readyState === WebSocket.OPEN,
    send: (frame) => socket.send(frame),
    waitAck,
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    now: () => performance.now(),
    log,
  });
};

const relay = async (event: ParsedEvent, socket: WebSocket): Promise<void> => {
  const id = ++requestSeq;
  const started = performance.now();
  const elapsed = (): string => `${Math.round(performance.now() - started)}ms`;
  // scene 只认三个值：拿不到 id 就是 unknown，那种消息后面一步都不会出站。
  const scene = sceneOf(event.where);
  const target = targetOf(event.where);
  const key = dedupKey({
    messageId: event.messageId,
    scene,
    target,
    whoId: event.who.id,
    time: event.time,
    text: event.text,
  });

  // 认领放在调核之前、而且是同步的：账要等 ack 才落行，
  // 光查账的话「已认领、未落账」这段窗口里 key 是隐形的，同一条会被领两次、发两遍。
  if (!ledger.claim(key)) {
    const state = ledger.firstKind(key);
    const note = dedupNote(state);
    log(
      "dedup",
      `key=${key} ${state === null || state === "claimed" ? "同一条还在处理中" : `已有记录 kind=${state}`}，本次丢弃（不调核、不发）`,
    );
    ledger.appendNew({ kind: "dup", mode: gate.mode, key, messageId: event.messageId, scene, target, note });
    return;
  }

  try {
    await processClaimed(event, socket, { id, key, scene, target, elapsed });
  } catch (error) {
    // 认领了就必须收尾：异常若把 key 永久占住，这条消息以后再也进不来，账上也会缺一行。
    // 已经落过真行（比如 sent 了一些泡）就不补记，行数规则只认那一批。
    const state = ledger.firstKind(key);
    if (needsErrorRow(state)) {
      ledger.appendNew({
        kind: "error",
        mode: gate.mode,
        key,
        messageId: event.messageId,
        scene,
        target,
        note: `处理异常：${(error as Error).message}`,
      });
    }
    log("error", `req#${id} 处理异常，本轮收尾：${(error as Error).message}`);
  } finally {
    // 无论走到哪条分支都放掉在飞标记：它只是互斥用的，不是历史事实。
    ledger.release(key);
  }
};

const handleEvent = (socket: WebSocket, raw: string): void => {
  eventSeq += 1;
  const id = eventSeq;
  log("recv", `#${id} 原始事件 ${raw}`);

  const parsed = parseEvent(raw);
  if (!parsed.ok) {
    log("parse", `#${id} 解析失败：${parsed.reason}`);
    return;
  }
  log(
    "parse",
    `#${id} ok who=[${describeWho(parsed)}] where=[${describeWhere(parsed)}] ` +
      `text=${JSON.stringify(parsed.text)} hasImage=${parsed.hasImage}`,
  );
  if (CORE_URL) {
    // 不 await：一个会话卡在等 ack 上，不该挡住其他人说话。
    void relay(parsed, socket).catch((error: Error) => log("error", `#${id} 处理失败：${error.message}`));
  }
};

const wss = new WebSocketServer({ host: HOST, port: PORT, path: WS_PATH });

// 监听是否已经成功：监听阶段出错（典型 EADDRINUSE）与运行期出错是两回事，不能混成一句话。
let listening = false;

wss.on("listening", () => {
  listening = true;
  log("listen", `反向 WS 已监听 ws://${HOST}:${PORT}${WS_PATH}，等客户端连进来`);
  log(
    "mode",
    CORE_URL
      ? `M2：核地址 ${CORE_URL}${CORE_PATH}，超时=${CORE_TIMEOUT.ms}ms，收到消息会转给核并回话`
      : "M1：只记日志，不发任何东西",
  );
});

wss.on("connection", (socket, request) => {
  const id = ++connectionSeq;
  const peer = `${request.socket.remoteAddress}:${request.socket.remotePort}`;
  log("conn", `#${id} 已连接 来自 ${peer}，当前在线 ${wss.clients.size}`);

  socket.on("message", (data) => {
    const raw = data.toString();
    const ack = isActionAck(raw);
    if (ack !== null) {
      const waiter = ackWaiters.get(ack.echo);
      if (waiter) {
        ackWaiters.delete(ack.echo);
        waiter({ kind: "ack", status: ack.status, retcode: ack.retcode, note: ack.note });
      }
      return;
    }
    handleEvent(socket, raw);
  });

  socket.on("close", (code, reason) => {
    log("conn", `#${id} 已断开 code=${code} reason=${reason.toString() || "（无）"}，当前在线 ${wss.clients.size}`);
    // 连接没了就别让还在等 ack 的泡空等到超时：它已经「不确定」了，早说早收尾。
    for (const [echo, waiter] of ackWaiters) {
      ackWaiters.delete(echo);
      waiter({ kind: "closed" });
    }
  });

  socket.on("error", (error) => {
    log("conn", `#${id} 连接出错：${error.message}`);
  });
});

wss.on("error", (error) => {
  if (listening) {
    log("error", `服务出错：${error.message}`);
    return;
  }
  // 端口没拿到就是没上线：说清楚原因，并且真的退出（退出码 1，便于外部脚本判断）。
  log("error", `监听失败：${error.message}，进程退出`);
  process.stdout.write("", () => process.exit(1));
});

process.on("SIGINT", () => {
  log("exit", "收到 Ctrl-C，关闭监听");
  wss.close(() => process.exit(0));
});
