// OneBot v11 反向 WS 通道：我们开 127.0.0.1:6200，协议端（"会说 OneBot 的进程"）连进 /ws。
// M1（默认）：收到消息只记日志——原始事件 + parseEvent 解析结果，不发任何东西。
// M2（加 --core <核地址>）：消息转给核 POST /inbound，把核回的字符串用 OneBot action 发回来源会话。
// 解析不在这里重写：一律走 M0 已验收的 parse.ts。
import WebSocket, { WebSocketServer } from "ws";
import { parseEvent, type ParsedEvent } from "./parse.ts";

const HOST = "127.0.0.1";
const PORT = 6200;
const WS_PATH = "/ws";

// 不给 --core 就是纯 M1；给了才连核。
const coreArgIndex = process.argv.indexOf("--core");
const CORE_URL = coreArgIndex >= 0 ? (process.argv[coreArgIndex + 1] ?? "") : "";

const now = (): string => new Date().toISOString();
const log = (tag: string, message: string): void => {
  console.log(`${now()} ${tag.padEnd(6)} ${message}`);
};

const describeWho = (event: ParsedEvent): string => {
  const parts = [String(event.who.id)];
  if (event.who.card) parts.push(`名片=${event.who.card}`);
  if (event.who.nickname) parts.push(`昵称=${event.who.nickname}`);
  return parts.join(" ");
};

const describeWhere = (event: ParsedEvent): string =>
  event.where.scene === "group" ? `群 ${event.where.groupId}` : `私聊 ${event.where.userId}`;

let connectionSeq = 0;
let eventSeq = 0;
let requestSeq = 0;

// 往哪个会话回：群聊 send_group_msg，私聊 send_private_msg。内容就是核返回的那句话。
const buildAction = (event: ParsedEvent, text: string, echo: string): Record<string, unknown> => {
  const message = [{ type: "text", data: { text } }];
  if (event.where.scene === "group") {
    return { action: "send_group_msg", params: { group_id: event.where.groupId, message }, echo };
  }
  return { action: "send_private_msg", params: { user_id: event.where.userId, message }, echo };
};

const relay = async (event: ParsedEvent, socket: WebSocket): Promise<void> => {
  const id = ++requestSeq;
  const started = performance.now();
  const elapsed = (): string => `${Math.round(performance.now() - started)}ms`;

  log(`req#${id}`, `① 收到 ${describeWho(event)} @ ${describeWhere(event)} text=${JSON.stringify(event.text)}`);
  log(`req#${id}`, `② 交给核 POST ${CORE_URL}/inbound`);

  let text: string;
  try {
    const response = await fetch(`${CORE_URL}/inbound`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        who: event.who,
        where: event.where,
        text: event.text,
        hasImage: event.hasImage,
        messageId: event.messageId,
      }),
      signal: AbortSignal.timeout(5000),
    });
    const payload = (await response.json()) as { text?: unknown };
    if (typeof payload.text !== "string") {
      log(`req#${id}`, `② 核的响应里没有 text（HTTP ${response.status}），本轮不回（用时 ${elapsed()}）`);
      return;
    }
    text = payload.text;
  } catch (error) {
    log(`req#${id}`, `② 核不可达，本轮不回：${(error as Error).message}（用时 ${elapsed()}）`);
    return;
  }
  log(`req#${id}`, `③ 核回 ${JSON.stringify(text)}（用时 ${elapsed()}）`);

  if (socket.readyState !== WebSocket.OPEN) {
    log(`req#${id}`, `④ 连接已断，本轮不发（用时 ${elapsed()}）`);
    return;
  }
  const echo = `req-${id}`;
  const action = buildAction(event, text, echo);
  socket.send(JSON.stringify(action));
  log(`req#${id}`, `④ 发出 ${String(action.action)} echo=${echo}（用时 ${elapsed()}）`);
};

// 协议端对我们下发 action 的应答：{status, retcode, data, echo}，没有 post_type。
const isActionAck = (raw: string): boolean => {
  let frame: unknown;
  try {
    frame = JSON.parse(raw);
  } catch {
    return false;
  }
  if (typeof frame !== "object" || frame === null) return false;
  const record = frame as Record<string, unknown>;
  if ("post_type" in record) return false;
  if (!("echo" in record) || !("retcode" in record)) return false;
  log(
    "ack",
    `收到下发应答 echo=${JSON.stringify(record.echo)} status=${String(record.status)} retcode=${String(record.retcode)}`,
  );
  return true;
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
  if (CORE_URL) void relay(parsed, socket);
};

const wss = new WebSocketServer({ host: HOST, port: PORT, path: WS_PATH });

// 监听是否已经成功：监听阶段出错（典型 EADDRINUSE）与运行期出错是两回事，不能混成一句话。
let listening = false;

wss.on("listening", () => {
  listening = true;
  log("listen", `反向 WS 已监听 ws://${HOST}:${PORT}${WS_PATH}，等客户端连进来`);
  log("mode", CORE_URL ? `M2：核地址 ${CORE_URL}，收到消息会转给核并回话` : "M1：只记日志，不发任何东西");
});

wss.on("connection", (socket, request) => {
  const id = ++connectionSeq;
  const peer = `${request.socket.remoteAddress}:${request.socket.remotePort}`;
  log("conn", `#${id} 已连接 来自 ${peer}，当前在线 ${wss.clients.size}`);

  socket.on("message", (data) => {
    const raw = data.toString();
    if (isActionAck(raw)) return;
    handleEvent(socket, raw);
  });

  socket.on("close", (code, reason) => {
    log("conn", `#${id} 已断开 code=${code} reason=${reason.toString() || "（无）"}，当前在线 ${wss.clients.size}`);
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
