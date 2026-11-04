// 假协议端（本机自测用）：连到我们的反向 WS，按 OneBot v11 发事件；
// 若我们下发 action，就照 OneBot v11 协议回一条带 echo 的应答。
// 只认 OneBot v11 标准字段，不 import 任何协议端 SDK。
//
// 用法：
//   node channels/onebot/tools/fake-client.ts
//   node channels/onebot/tools/fake-client.ts --events event.private.text.json --wait 3000
//   node channels/onebot/tools/fake-client.ts --url ws://127.0.0.1:6200/ws --interval 800 --wait 4000
import WebSocket from "ws";
import { readFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(HERE, "..", "fixtures");
const DEFAULT_EVENTS = ["event.group.text.json", "event.private.text.json", "event.group.image.json"];

const arg = (name: string, fallback: string): string => {
  const index = process.argv.indexOf(`--${name}`);
  const value = index >= 0 ? process.argv[index + 1] : undefined;
  return value && !value.startsWith("--") ? value : fallback;
};

const url = arg("url", "ws://127.0.0.1:6200/ws");
const events = arg("events", DEFAULT_EVENTS.join(",")).split(",").filter((name) => name.length > 0);
const intervalMs = Number(arg("interval", "800"));
const dwellMs = Number(arg("wait", "4000"));

const now = (): string => new Date().toISOString();
const log = (message: string): void => {
  console.log(`${now()} [fake] ${message}`);
};

// 文件里的 JSON 压成一行再发：事件内容不变，但服务端日志里一条事件正好一行。
const readEvent = (name: string): string => {
  const path = isAbsolute(name) ? name : join(FIXTURES, name);
  return JSON.stringify(JSON.parse(readFileSync(path, "utf8")));
};

// OneBot v11 应答帧：{status, retcode, data, echo}
const isActionFrame = (value: unknown): value is { action: string; params?: unknown; echo?: unknown } => {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.action === "string";
};

const socket = new WebSocket(url);
let sent = 0;
let received = 0;
let ackMessageId = 900000;

socket.on("open", () => {
  log(`已连上 ${url}`);
  sendNext();
});

const sendNext = (): void => {
  if (sent >= events.length) {
    if (dwellMs > 0) {
      log(`事件已发完（${sent} 条），停留 ${dwellMs}ms 等下发`);
      setTimeout(() => {
        log(`停留结束，主动断开`);
        socket.close();
      }, dwellMs);
    } else {
      log("事件已发完，常驻等待（Ctrl-C 退出）");
    }
    return;
  }
  const name = events[sent];
  const raw = readEvent(name);
  socket.send(raw);
  sent += 1;
  log(`发出 → ${name}（${raw.length} 字节）`);
  setTimeout(sendNext, intervalMs);
};

socket.on("message", (data) => {
  const raw = data.toString();
  received += 1;
  log(`收到 ← ${raw}`);
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
  const ack = {
    status: "ok",
    retcode: 0,
    data: { message_id: ++ackMessageId },
    echo: frame.echo ?? null,
  };
  socket.send(JSON.stringify(ack));
  log(`应答 → ${JSON.stringify(ack)}`);
});

socket.on("close", (code) => {
  log(`连接已断开 code=${code}；共发出 ${sent} 条事件，收到 ${received} 条下发`);
  process.exit(0);
});

socket.on("error", (error) => {
  log(`连接出错：${error.message}`);
  process.exit(1);
});
