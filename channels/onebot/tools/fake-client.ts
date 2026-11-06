// 假协议端（本机自测用）：连到我们的反向 WS，按 OneBot v11 发事件；
// 若我们下发 action，就照 OneBot v11 协议回一条带 echo 的应答。
// 只认 OneBot v11 标准字段，不 import 任何协议端 SDK。
//
// 它同时是「不听话的协议端」：可以故意回失败码、故意慢回、故意把同一条事件投两次、
// 故意断开重连——这些都是通道必须扛住的输入，不该靠手动改代码来造。
//
// 用法：
//   node channels/onebot/tools/fake-client.ts
//   node channels/onebot/tools/fake-client.ts --events event.private.text.json --wait 3000
//   node channels/onebot/tools/fake-client.ts --url ws://127.0.0.1:6299/ws --interval 800 --wait 4000
//
// 不听话的几种：
//   --ack-status <ok|failed>   应答里的 status，默认 ok
//   --ack-retcode <n>          应答里的 retcode，默认 0；非 0 即「协议端拒绝」
//   --ack-delay <ms>           延迟多久才回应答，默认 0；调大即「慢协议端」（验 ack 超时）
//   --ack-message <文本>       塞进 data.message / 顶层 message，验日志有没有保留原始错误码
//   --duplicate                每条事件紧跟着再投一次（同 message_id，验去重）
//   --reconnect [ms]           一轮发完后断开，默认 1000ms 后重连并把同一批事件再发一轮（验去重不随连接丢）
//   --help
import WebSocket from "ws";
import { readFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(HERE, "..", "fixtures");
const DEFAULT_EVENTS = ["event.group.text.json", "event.private.text.json", "event.group.image.json"];

const arg = (name: string, fallback: string): string => {
  const index = process.argv.indexOf("--" + name);
  const value = index >= 0 ? process.argv[index + 1] : undefined;
  return value && !value.startsWith("--") ? value : fallback;
};
const flag = (name: string): boolean => process.argv.includes("--" + name);

if (flag("help")) {
  console.log(
    [
      "fake-client.ts —— 假协议端（也是不听话的协议端）",
      "",
      "基础参数（默认值即老行为）：",
      "  --url <ws url>       默认 ws://127.0.0.1:6299/ws",
      "  --events <a,b,c>     fixture 文件名或绝对路径，逗号分隔；默认三个 fixture",
      "  --interval <ms>      事件之间的间隔，默认 800",
      "  --wait <ms>          发完停留多久再断开，默认 4000（0 = 常驻）",
      "",
      "不听话的参数：",
      "  --ack-status <s>     应答 status，默认 ok（可填 failed）",
      "  --ack-retcode <n>    应答 retcode，默认 0",
      "  --ack-delay <ms>     延迟应答，默认 0",
      "  --ack-message <文本> 塞进 data.message 与顶层 message",
      "  --duplicate          每条事件紧接着重投一次（同 message_id）",
      "  --reconnect [ms]     一轮发完后断开，默认 1000ms 后重连再发一轮",
      "  --help",
      "",
      "例：",
      "  node channels/onebot/tools/fake-client.ts --ack-retcode 100 --ack-message 权限不足",
      "  node channels/onebot/tools/fake-client.ts --ack-delay 12000        # 逼出 ack 超时",
      "  node channels/onebot/tools/fake-client.ts --duplicate --reconnect",
    ].join("\n"),
  );
  process.exit(0);
}

const url = arg("url", "ws://127.0.0.1:6299/ws");
const events = arg("events", DEFAULT_EVENTS.join(",")).split(",").filter((name) => name.length > 0);
const intervalMs = Number(arg("interval", "800"));
const dwellMs = Number(arg("wait", "4000"));
const ackStatus = arg("ack-status", "ok");
const ackRetcode = Number(arg("ack-retcode", "0"));
const ackDelayMs = Number(arg("ack-delay", "0"));
const ackMessage = arg("ack-message", "");
const duplicate = flag("duplicate");
const reconnectMs = flag("reconnect") ? Number(arg("reconnect", "1000")) : 0;
// 两轮 = 发一批 → 断开重连 → 同一批再发一次；第二轮的全部事件都该被去重挡住。
const rounds = reconnectMs > 0 ? 2 : 1;

const now = (): string => new Date().toISOString();
const log = (message: string): void => {
  console.log(now() + " [fake] " + message);
};

// 文件里的 JSON 压成一行再发：事件内容不变，但服务端日志里一条事件正好一行。
const readEvent = (name: string): string => {
  const path = isAbsolute(name) ? name : join(FIXTURES, name);
  return JSON.stringify(JSON.parse(readFileSync(path, "utf8")));
};

const isActionFrame = (value: unknown): value is { action: string; params?: unknown; echo?: unknown } => {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.action === "string";
};

let socket: WebSocket | null = null;
let round = 0;
let index = 0;
let sentTotal = 0;
let received = 0;
let reconnecting = false;
let ackMessageId = 900000;

// 应答帧照 OneBot v11：{status, retcode, data, echo}，没有 post_type。
// 失败时把原始原因带回去（data.message 与顶层 message），否则通道日志里只剩一个数字。
const buildAck = (frame: { echo?: unknown }): Record<string, unknown> => {
  const data: Record<string, unknown> = { message_id: ++ackMessageId };
  if (ackMessage) data.message = ackMessage;
  const ack: Record<string, unknown> = { status: ackStatus, retcode: ackRetcode, data, echo: frame.echo ?? null };
  if (ackRetcode !== 0 && ackMessage) ack.message = ackMessage;
  return ack;
};

const sendAck = (ack: Record<string, unknown>): void => {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    log("连接已断，应答发不出去");
    return;
  }
  socket.send(JSON.stringify(ack));
  log("应答 → " + JSON.stringify(ack));
};

const finishRound = (): void => {
  if (round < rounds) {
    log("第 " + round + "/" + rounds + " 轮事件已发完，" + dwellMs + "ms 后断开重连");
    setTimeout(() => {
      reconnecting = true;
      socket?.close();
    }, dwellMs);
    return;
  }
  if (dwellMs > 0) {
    log("事件已发完（共 " + sentTotal + " 条），停留 " + dwellMs + "ms 等下发");
    setTimeout(() => {
      log("停留结束，主动断开");
      socket?.close();
    }, dwellMs);
  } else {
    log("事件已发完，常驻等待（Ctrl-C 退出）");
  }
};

const sendNext = (): void => {
  if (index >= events.length) {
    finishRound();
    return;
  }
  const name = events[index];
  const raw = readEvent(name);
  index += 1;
  sentTotal += 1;
  socket?.send(raw);
  log("发出 → " + name + "（" + raw.length + " 字节）");
  if (duplicate) {
    sentTotal += 1;
    socket?.send(raw);
    log("重投 → " + name + "（同一条事件第二次，同 message_id，用来验去重）");
  }
  setTimeout(sendNext, intervalMs);
};

const connect = (): void => {
  socket = new WebSocket(url);
  const current = socket;

  current.on("open", () => {
    round += 1;
    index = 0;
    log("已连上 " + url + "（第 " + round + "/" + rounds + " 轮）");
    sendNext();
  });

  current.on("message", (data) => {
    const raw = data.toString();
    received += 1;
    log("收到 ← " + raw);
    let frame: unknown;
    try {
      frame = JSON.parse(raw);
    } catch {
      log("收到的东西不是 JSON，忽略");
      return;
    }
    if (!isActionFrame(frame)) {
      log("收到的不是 action 帧，忽略");
      return;
    }
    const ack = buildAck(frame);
    if (ackDelayMs > 0) {
      log("故意慢 " + ackDelayMs + "ms 再应答（模拟慢协议端）");
      setTimeout(() => sendAck(ack), ackDelayMs);
      return;
    }
    sendAck(ack);
  });

  current.on("close", (code) => {
    log("连接已断开 code=" + code + "；共发出 " + sentTotal + " 条事件，收到 " + received + " 条下发");
    if (reconnecting && round < rounds) {
      reconnecting = false;
      log(reconnectMs + "ms 后重连（第 " + (round + 1) + "/" + rounds + " 轮会把同一批事件再发一次）");
      setTimeout(connect, reconnectMs);
      return;
    }
    process.exit(0);
  });

  current.on("error", (error) => {
    log("连接出错：" + error.message);
    // 重连窗口里的报错由 close 分支接手，不要在这里把进程打死。
    if (!reconnecting) process.exit(1);
  });
};

connect();
