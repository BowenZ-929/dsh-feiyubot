// drill.ts —— 一条命令的断言式演练：它扮演协议端，按冻结契约给通道下断言。
// 前提（A–F 组）：通道已在 --url 跑着、核也已在跑（drill 自己不起进程、不发 HTTP）。
// 唯一例外是 G 组（--batch）：合批要控制核回什么、门开不开、配额剩几次，
// 所以 G 组自己起一个假核 + 一个独用端口的通道，跑完就关。
// 它做六件事：投一条事件 → 看有没有出站 → 校验出站形态并应答 → 重投/重连看有没有被去重挡住
// → 拿账里「本次运行之前就存在」的旧 key 重投一次（去重认的是盘上的账，进程重启后也一样）
// → 在飞窗口重投一次（第一批还没发完就重投，验认领是调核前的同步占位，而不是等 ack 落账）；
// 最后打印结论表，全 PASS 退出码 0，否则 1。
//
// 用法（--help 也能看）：
//   node channels/onebot/tools/drill.ts --expect-mode silent
//   node channels/onebot/tools/drill.ts --expect-mode send
//   node channels/onebot/tools/drill.ts --expect-mode send --fail-retcode 100
//   node channels/onebot/tools/drill.ts --expect-mode send --reconnect --wait 10000
//   node channels/onebot/tools/drill.ts --expect-mode silent --once
//
// --wait 是「一个观察窗口」的毫秒数：窗口内不早退，才能真的断言「一个 action 都没有」。
// 发送模式下窗口必须盖住整批节奏（send_at 的首泡到末泡），默认 8000ms 够 [0,1.6,3.2] 这种排布。
import WebSocket from "ws";
import { readFileSync, existsSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer as createHttpServer } from "node:http";
import { connect as netDial, createServer as createNetServer } from "node:net";
import { loadPolicy } from "../policy.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(HERE, "..", "fixtures");
const DEFAULT_LEDGER = resolve(HERE, "..", ".state", "ledger.jsonl");

const now = (): string => new Date().toISOString();
const say = (message: string): void => console.log(now() + " [drill] " + message);
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

// ---------- 参数 ----------

export type Config = {
  url: string;
  expectMode: "silent" | "send";
  scene: "group" | "private";
  groupId: number;
  userId: number;
  wait: number;
  failRetcode: number | null;
  reconnect: boolean;
  ledger: string;
  once: boolean;
  inflight: boolean;
  channelLog: string | null;
  // G 组（合批）：自成一体地起假核 + 通道，只跑 G 断言。
  batch: boolean;
  decisions: string;
  help: boolean;
};

const raw = (argv: string[], name: string): string | undefined => {
  const index = argv.indexOf("--" + name);
  if (index < 0) return undefined;
  const value = argv[index + 1];
  return value && !value.startsWith("--") ? value : undefined;
};

const has = (argv: string[], name: string): boolean => argv.includes("--" + name);

// M4 起每个群有 1 小时配额：群号必须和 messageId 一样"每次新造"，否则一小时内的第二遍演练
// 会被 quota_exceeded 挡住（那不是回归，是配额在按设计工作）。
const freshGroupId = (): number => 700_000_000 + ((Date.now() % 1_000_000) * 100 + (process.pid % 100));

export const parseConfig = (argv: string[]): Config => {
  const expectRaw = raw(argv, "expect-mode") ?? "silent";
  const expectMode = expectRaw === "send" ? "send" : "silent";
  const privateMode = has(argv, "private");
  const failRetcodeRaw = raw(argv, "fail-retcode");
  const failRetcode = failRetcodeRaw === undefined ? null : Number(failRetcodeRaw);
  return {
    url: raw(argv, "url") ?? "ws://127.0.0.1:6200/ws",
    expectMode,
    scene: privateMode ? "private" : "group",
    groupId: Number(raw(argv, "group") ?? String(freshGroupId())),
    userId: Number(raw(argv, "user") ?? "3000000003"),
    wait: Number(raw(argv, "wait") ?? "8000"),
    failRetcode: failRetcode !== null && Number.isFinite(failRetcode) ? failRetcode : null,
    reconnect: has(argv, "reconnect"),
    ledger: raw(argv, "ledger") ?? DEFAULT_LEDGER,
    once: has(argv, "once"),
    // 断言 F 默认跑；--no-inflight 关掉（不想让 drill 在通道里制造在飞并发时用）。
    inflight: !has(argv, "no-inflight"),
    // 断言 F4 想真数 plan 行，就把通道的 stdout 文件指过来；不给就按 echo 请求号反推。
    channelLog: raw(argv, "channel-log") ?? null,
    // G 组（合批）自成一体：自己起假核 + 通道，不碰 --url 上那个。
    batch: has(argv, "batch"),
    decisions: raw(argv, "decisions") ?? DEFAULT_DECISIONS,
    help: has(argv, "help"),
  };
};

export const printHelp = (): void => {
  const lines = [
    "drill.ts —— 扮演协议端，对运行中的 OneBot 通道下断言（不出站 / 分泡与 ack / 去重 / 重连去重）",
    "",
    "参数：",
    "  --expect-mode silent|send  通道应有的模式（决定跑哪组断言），默认 silent",
    "  --url <ws url>             通道地址，默认 ws://127.0.0.1:6200/ws",
    "  --group <id>               群号（群消息的目标），默认每次新造（M4 有每群 1 小时配额）",
    "  --user <id>                私聊对象 QQ 号，默认 3000000003",
    "  --private                  改跑私聊事件（用的是 fixtures/event.private.text.json 的结构）",
    "  --wait <ms>                每个观察窗口的毫秒数，默认 8000；窗口内不早退",
    "  --fail-retcode <n>         收到 action 就回 retcode=<n> 的失败 ack，断言账里 kind=failed 且 retcode=n",
    "  --reconnect                中间断开 1s 再重连，把同一 messageId 投第三次，断言仍不出站且 dup 再 +1",
    "  --ledger <path>            账文件，默认 channels/onebot/.state/ledger.jsonl",
    "  --once                     只跑第一条断言（不跑去重、重连、旧账重投、在飞窗口重投）",
    "                             断言 E 自动跑：拿账里早已存在的 key 重投，验去重认盘不认内存",
    "                             断言 F 自动跑（仅 send）：第一批还没发完就重投，验认领是同步占位",
    "  --no-inflight              关掉断言 F（不想在通道里制造在飞并发时）",
    "  --channel-log <path>       通道的 stdout 文件；给了就真数 plan 行（F4），不给按 echo 请求号反推",
    "  --batch                    只跑 G 组（合批）：drill 自己起假核 + 独用端口的通道，不碰 --url 上那个",
    "  --decisions <path>         G 组读的决策账，默认 channels/onebot/.state/decisions.jsonl",
    "  --help                     打印这段说明",
    "",
    "退出码：全 PASS 为 0，任何一条 FAIL 或连不上为 1。",
    "",
    "例：",
    "  node channels/onebot/tools/drill.ts --expect-mode silent",
    "  node channels/onebot/tools/drill.ts --expect-mode send --wait 12000",
    "  node channels/onebot/tools/drill.ts --expect-mode send --fail-retcode 100",
    "  node channels/onebot/tools/drill.ts --expect-mode send --reconnect",
  ];
  console.log(lines.join("\n"));
};

// ---------- 纯函数（可单独测：不碰网络、不碰时钟、不碰文件） ----------

export type EventOptions = {
  scene: "group" | "private";
  groupId: number;
  userId: number;
  messageId: number;
  time: number;
};

// 用 fixture 的真实结构，只改「这次唯一的」几个字段：message_id/time 必须唯一，否则会撞上次的账。
export const buildEvent = (fixture: Record<string, unknown>, opts: EventOptions): Record<string, unknown> => {
  const event = JSON.parse(JSON.stringify(fixture)) as Record<string, any>;
  event.time = opts.time;
  event.message_id = opts.messageId;
  event.message_seq = opts.messageId;
  if (opts.scene === "group") {
    event.message_type = "group";
    event.group_id = opts.groupId;
  } else {
    event.message_type = "private";
    event.user_id = opts.userId;
    if (event.sender && typeof event.sender === "object") event.sender.user_id = opts.userId;
  }
  return event;
};

export type LedgerRow = {
  ts?: string;
  kind?: string;
  mode?: string;
  key?: string;
  messageId?: number | null;
  scene?: string;
  target?: string;
  index?: number | null;
  text?: string | null;
  send_at?: number | null;
  retcode?: number | null;
  status?: string | null;
  note?: string;
};

export const readLedgerRows = (text: string): LedgerRow[] => {
  const rows: LedgerRow[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const value = JSON.parse(trimmed);
      if (value && typeof value === "object" && !Array.isArray(value)) rows.push(value as LedgerRow);
    } catch {
      // 半行/坏行跳过：账是追加写的，读的瞬间可能有半行，不该让演练因此崩。
    }
  }
  return rows;
};

export const rowsFor = (rows: LedgerRow[], key: string): LedgerRow[] => rows.filter((row) => row.key === key);
export const countKind = (rows: LedgerRow[], kind: string): number => rows.filter((row) => row.kind === kind).length;
export const countKindFor = (rows: LedgerRow[], key: string, kind: string): number => countKind(rowsFor(rows, key), kind);

export const isActionFrame = (value: unknown): boolean =>
  typeof value === "object" && value !== null && typeof (value as Record<string, unknown>).action === "string";

export type Expect = { scene: "group" | "private"; targetId: number };

// 一条 action 帧是否完全符合契约：动作名、目标 id、message 是 text 段数组、echo 形态。
export const validateAction = (frame: Record<string, any>, expect: Expect): string[] => {
  const errors: string[] = [];
  if (!isActionFrame(frame)) return ["不是 action 帧（缺 action 字符串字段）"];
  const wantAction = expect.scene === "group" ? "send_group_msg" : "send_private_msg";
  if (frame.action !== wantAction) errors.push("action=" + String(frame.action) + "，应为 " + wantAction);

  const params = frame.params;
  if (typeof params !== "object" || params === null) return errors.concat("params 不是对象");
  if (expect.scene === "group") {
    if (params.group_id !== expect.targetId) errors.push("params.group_id=" + JSON.stringify(params.group_id) + "，应为 " + expect.targetId);
  } else if (params.user_id !== expect.targetId) {
    errors.push("params.user_id=" + JSON.stringify(params.user_id) + "，应为 " + expect.targetId);
  }

  if (!Array.isArray(params.message)) return errors.concat("params.message 不是数组");
  if (params.message.length === 0) errors.push("params.message 是空数组");
  params.message.forEach((segment: any, index: number) => {
    if (typeof segment !== "object" || segment === null) {
      errors.push("message[" + index + "] 不是对象");
      return;
    }
    if (segment.type !== "text") errors.push("message[" + index + "].type=" + JSON.stringify(segment.type) + "，应为 text");
    if (typeof segment.data?.text !== "string") errors.push("message[" + index + "].data.text 不是字符串");
  });

  if (typeof frame.echo !== "string") errors.push("echo 不是字符串：" + JSON.stringify(frame.echo));
  else if (!/^req-\d+-\d+$/.test(frame.echo)) errors.push("echo=" + JSON.stringify(frame.echo) + " 不符合 req-<n>-<i>");
  return errors;
};

export const echoParts = (echo: string): { req: number; index: number } | null => {
  const matched = /^req-(\d+)-(\d+)$/.exec(echo);
  return matched ? { req: Number(matched[1]), index: Number(matched[2]) } : null;
};

// 一批泡的 echo 必须同请求号、泡序号从 0 起严格递增。
export const checkEchoSequence = (echoes: string[]): string[] => {
  const parts = echoes.map(echoParts);
  if (parts.length === 0) return ["没有收到任何 echo"];
  if (parts.some((part) => part === null)) return ["有 echo 不符合 req-<n>-<i>"];
  const errors: string[] = [];
  const first = parts[0]!;
  parts.forEach((part, index) => {
    if (part!.req !== first.req) errors.push("第 " + index + " 泡的请求号=" + part!.req + "，与首泡 " + first.req + " 不一致");
    if (part!.index !== index) errors.push("第 " + index + " 泡的泡序号=" + part!.index + "，应为 " + index);
  });
  return errors;
};

// 应答帧：成功给 data.message_id，失败给 retcode 原值且不带 data。
export const buildAck = (frame: Record<string, any>, failRetcode: number | null, messageId: number): Record<string, unknown> => {
  if (failRetcode === null) {
    return { status: "ok", retcode: 0, data: { message_id: messageId }, echo: frame?.echo ?? null };
  }
  return { status: "failed", retcode: failRetcode, data: {}, echo: frame?.echo ?? null };
};

// 表格按显示宽度对齐：中文占两列，否则结论表会歪。
const displayWidth = (text: string): number => {
  let width = 0;
  for (const char of text) width += /[\u1100-\u115f\u2e80-\ua4cf\ua960-\ua97f\uac00-\ud7a3\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff00-\uff60\uffe0-\uffe6]/.test(char) ? 2 : 1;
  return width;
};
const padRight = (text: string, width: number): string => text + " ".repeat(Math.max(0, width - displayWidth(text)));

// ---------- 运行时 ----------

type Check = { name: string; expect: string; actual: string; pass: boolean };

const checks: Check[] = [];
const add = (name: string, expect: string, pass: boolean, actual: string): void => {
  checks.push({ name, expect, actual, pass });
  say((pass ? "PASS" : "FAIL") + " " + name + "｜期望=" + expect + "｜实测=" + actual);
};

let socket: any = null;
const inbox: unknown[] = [];
// 与 inbox 同下标：每帧到达的墙钟时刻。G4 的「@ 到出手」就靠它算，不靠日志。
const frameTimes: number[] = [];
let ackEnabled = false;
let ackSeq = 900000;
// 断言 F 的两个旋钮：acks 故意慢回（让这一批保持在飞），并在收到第一帧时立刻重投同一条。
let ackDelayMs = 0;
let inflightReplay: Record<string, unknown> | null = null;
let inflightReplayed = false;

const mark = (): number => inbox.length;
const framesSince = (index: number): unknown[] => inbox.slice(index);
const actionsSince = (index: number): Record<string, any>[] =>
  framesSince(index).filter(isActionFrame) as Record<string, any>[];

const onMessage = (data: any): void => {
  const text = data.toString();
  let frame: any;
  try {
    frame = JSON.parse(text);
  } catch {
    inbox.push({ __unparsed: text });
    frameTimes.push(Date.now());
    say("收到非 JSON 帧：" + text.slice(0, 120));
    return;
  }
  inbox.push(frame);
  frameTimes.push(Date.now());
  if (!isActionFrame(frame)) {
    say("收到非 action 帧：" + text.slice(0, 160));
    return;
  }
  if (!ackEnabled) {
    // 该模式下一个 action 都不该有；故意不应答，让违约更刺眼（通道侧会因此 ack 超时）。
    say("!! 收到不该出现的 action 帧（不应答）" + text.slice(0, 200));
    return;
  }
  // 在飞窗口：第一帧一到就立刻重投同一条——这一刻通道那边一条账行都还没落。
  if (inflightReplay !== null && !inflightReplayed) {
    inflightReplayed = true;
    socket.send(JSON.stringify(inflightReplay));
    say("在飞重投 → 收到第一帧就立刻再投同一条（不等 ack）");
  }
  const ack = buildAck(frame, failRetcode, ++ackSeq);
  if (ackDelayMs > 0) {
    // 慢慢回 ack，这一批才会一直停在「已认领、未落账」的窗口里。
    setTimeout(() => {
      try {
        socket?.send(JSON.stringify(ack));
      } catch {
        // 连接没了就算了：这个 ack 只是为了撑住窗口
      }
    }, ackDelayMs);
    say("ack 延后 " + ackDelayMs + "ms → echo=" + JSON.stringify(frame.echo) + "（让这一批保持在飞）");
    return;
  }
  socket.send(JSON.stringify(ack));
  say("ack → echo=" + JSON.stringify(frame.echo) + " status=" + ack.status + " retcode=" + ack.retcode);
};

let failRetcode: number | null = null;

const connect = (url: string): Promise<any> =>
  new Promise((resolvePromise, rejectPromise) => {
    const ws = new WebSocket(url);
    const timer = setTimeout(() => {
      try {
        ws.terminate();
      } catch {
        /* 已经废了就算了 */
      }
      rejectPromise(new Error("连 " + url + " 超时（5s）"));
    }, 5000);
    ws.on("open", () => {
      clearTimeout(timer);
      ws.on("message", onMessage);
      resolvePromise(ws);
    });
    ws.on("error", (error) => {
      clearTimeout(timer);
      rejectPromise(error);
    });
  });

const readRowsFile = (path: string): LedgerRow[] =>
  existsSync(path) ? readLedgerRows(readFileSync(path, "utf8")) : [];

const waitForRows = async (path: string, predicate: (rows: LedgerRow[]) => boolean, ms: number): Promise<LedgerRow[]> => {
  const deadline = Date.now() + ms;
  let rows = readRowsFile(path);
  while (!predicate(rows) && Date.now() < deadline) {
    await sleep(100);
    rows = readRowsFile(path);
  }
  return rows;
};

const describeRow = (row: LedgerRow | undefined): string =>
  row === undefined
    ? "（无该行）"
    : "{kind=" + String(row.kind) + " index=" + String(row.index) + " retcode=" + String(row.retcode) + " status=" + String(row.status) + " text=" + JSON.stringify(String(row.text ?? "").slice(0, 30)) + "}";

const printTable = (): boolean => {
  const widthA = Math.max(...checks.map((check) => displayWidth(check.name)), displayWidth("断言"));
  const widthB = Math.max(...checks.map((check) => displayWidth(check.expect)), displayWidth("期望"));
  const widthC = Math.max(...checks.map((check) => displayWidth(check.actual)), displayWidth("实测"));
  const rule = "-".repeat(widthA + widthB + widthC + 14);
  console.log("");
  console.log("结论表");
  console.log(rule);
  console.log(padRight("断言", widthA) + "  " + padRight("期望", widthB) + "  " + padRight("实测", widthC) + "  结果");
  console.log(rule);
  for (const check of checks) {
    console.log(
      padRight(check.name, widthA) + "  " + padRight(check.expect, widthB) + "  " + padRight(check.actual, widthC) + "  " + (check.pass ? "PASS" : "FAIL"),
    );
  }
  console.log(rule);
  const failed = checks.filter((check) => !check.pass);
  console.log("合计 " + checks.length + " 条：" + (checks.length - failed.length) + " PASS / " + failed.length + " FAIL");
  return failed.length === 0;
};

// ---------- G 组：合批（drill 自己起通道 + 假核） ----------
// A–F 组假设「通道已经跑着」；G 组不能这么假设——它要控制核回什么、门开不开、配额剩几次，
// 所以它自己起一个假核 + 一个独用端口的通道，跑完就关。这是 drill 里唯一会起进程的地方。
const CHANNEL_DIR = resolve(HERE, "..");
const SERVER_PATH = resolve(CHANNEL_DIR, "server.ts");
const DEFAULT_DECISIONS = resolve(CHANNEL_DIR, ".state", "decisions.jsonl");
const QUOTA_PATH = resolve(CHANNEL_DIR, ".state", "quota.json");

// 配额文件里这个群还剩几条时间戳：G11/G12 用它证明「refund 真退回去了 / silent 根本没扣」。
const quotaStamps = (groupId: number): number[] => {
  if (!existsSync(QUOTA_PATH)) return [];
  try {
    const state = JSON.parse(readFileSync(QUOTA_PATH, "utf8")) as { groups?: Record<string, unknown> };
    const list = state.groups?.["group:" + groupId];
    return Array.isArray(list) ? list.filter((item): item is number => typeof item === "number") : [];
  } catch {
    return [];
  }
};
// 消息里带这个标记，假核就回 600 字（验三道上限里的字上限：maxChars 是 512）。
const LONG_MARK = "LONGREPLY";
// 这个标记让假核回「主动沉默」（send:false + 理由）；EMPTY_MARK 回没有 send 字段的空 chunks（对照）。
const SILENT_MARK = "SILENTREPLY";
const EMPTY_MARK = "EMPTYREPLY";
// 这个标记让假核回**两泡**：G13 靠「协议端不回 ack」把两泡都卡在出站失败上（sent=0）。
const TWO_MARK = "TWOREPLY";
const SPEAKER_A = 3100000001;
const SPEAKER_B = 3100000002;
// 同人一档是 tiersMs.sameSpeaker=2000ms：正好 2s 会压在 deadline 上，胜负由定时器与网络抖动决定，
// 所以真跑时留 400ms 余量（判据要证的是「同人连说三句进一段」，不是抢那 1ms 的边界）。
const G1_GAP_MS = 1600;

type CoreHit = { t: number; body: Record<string, any> };

type DecisionRow = {
  ts?: string;
  kind?: string;
  scene?: string;
  human?: string;
  machine?: {
    batch?: { id?: string; msgs?: number; waited_ms?: number; tier?: string; at?: string };
    gates?: { name: string; hit: boolean }[];
    decided?: string;
    why?: string;
    reason_code?: string;
    scene?: string;
  };
};

const readDecisionRows = (path: string): DecisionRow[] => {
  const rows: DecisionRow[] = [];
  if (!existsSync(path)) return rows;
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const value = JSON.parse(trimmed);
      if (value && typeof value === "object" && !Array.isArray(value) && (value as DecisionRow).kind === "decision") {
        rows.push(value as DecisionRow);
      }
    } catch {
      // 半行（正在写）跳过：不该让演练因此崩。
    }
  }
  return rows;
};

// 决策账没有 target/groupId：只能按时间窗归到「刚刚这一段」。G 组各段串行跑，窗口是干净的。
const decisionRowsSince = (path: string, sinceWall: number): DecisionRow[] =>
  readDecisionRows(path).filter((row) => {
    const stamp = typeof row.ts === "string" ? Date.parse(row.ts) : Number.NaN;
    return Number.isFinite(stamp) && stamp >= sinceWall - 100;
  });

const waitForDecisionRows = async (
  path: string,
  sinceWall: number,
  want: number,
  ms: number,
): Promise<DecisionRow[]> => {
  const deadline = Date.now() + ms;
  let rows = decisionRowsSince(path, sinceWall);
  while (rows.length < want && Date.now() < deadline) {
    await sleep(50);
    rows = decisionRowsSince(path, sinceWall);
  }
  return rows;
};

type FakeCore = { url: string; hits: CoreHit[]; close: () => Promise<void> };

// 假核：记录每一具请求体（G 组的证据主要来自这里），并按消息里有没有 LONG_MARK 决定回多长。
const startFakeCore = async (): Promise<FakeCore> => {
  const hits: CoreHit[] = [];
  const server = createHttpServer((request, response) => {
    let raw = "";
    request.on("data", (chunk) => {
      raw += chunk.toString();
    });
    request.on("end", () => {
      let body: Record<string, any>;
      try {
        body = JSON.parse(raw) as Record<string, any>;
      } catch {
        body = { __unparsed: raw.slice(0, 200) };
      }
      hits.push({ t: Date.now(), body });
      const text = typeof body.text === "string" ? body.text : "";
      const payload = text.includes(LONG_MARK)
        ? { ok: true, chunks: [{ text: "长".repeat(600), gap_before: 0, send_at: 0 }] }
        : text.includes(SILENT_MARK)
          ? { ok: true, chunks: [], send: false, reason: "刚在忙" }
          : text.includes(EMPTY_MARK)
            ? { ok: true, chunks: [] }
            : text.includes(TWO_MARK)
              ? { ok: true, chunks: [{ text: "第一泡", gap_before: 0, send_at: 0 }, { text: "第二泡", gap_before: 0, send_at: 0 }] }
              : { ok: true, chunks: [{ text: "在的", gap_before: 0, send_at: 0 }] };
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify(payload));
    });
  });
  await new Promise<void>((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
  const address = server.address();
  const port = typeof address === "object" && address !== null ? address.port : 0;
  return {
    url: "http://127.0.0.1:" + port,
    hits,
    close: () => new Promise<void>((resolveClose) => server.close(() => resolveClose())),
  };
};

const freePort = async (): Promise<number> =>
  new Promise<number>((resolvePort, rejectPort) => {
    const probe = createNetServer();
    probe.once("error", rejectPort);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address();
      const port = typeof address === "object" && address !== null ? address.port : 0;
      probe.close(() => resolvePort(port));
    });
  });

const portIsOpen = (port: number): Promise<boolean> =>
  new Promise<boolean>((resolveProbe) => {
    const dialer = netDial({ port, host: "127.0.0.1" });
    dialer.setTimeout(400);
    dialer.once("connect", () => {
      dialer.destroy();
      resolveProbe(true);
    });
    dialer.once("timeout", () => {
      dialer.destroy();
      resolveProbe(false);
    });
    dialer.once("error", () => resolveProbe(false));
  });

type Channel = { port: number; url: string; dump: () => string; stop: () => Promise<void> };

// 起一个独用端口的通道：不抢线上那个 6200，也不受线上通道影响。
const startChannel = async (coreUrl: string, mode: "send" | "silent"): Promise<Channel> => {
  let lastDump = "";
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const port = await freePort();
    const child: ChildProcess = spawn(
      process.execPath,
      [SERVER_PATH, "--core", coreUrl, "--port", String(port)],
      { cwd: CHANNEL_DIR, env: { ...process.env, QQAGENT_CHANNEL_MODE: mode }, stdio: ["ignore", "pipe", "pipe"] },
    );
    let dump = "";
    child.stdout?.on("data", (chunk) => {
      dump += chunk.toString();
    });
    child.stderr?.on("data", (chunk) => {
      dump += chunk.toString();
    });
    let exited = false;
    child.once("exit", () => {
      exited = true;
    });
    const deadline = Date.now() + 5000;
    let ready = false;
    while (Date.now() < deadline && !exited) {
      if (await portIsOpen(port)) {
        ready = true;
        break;
      }
      await sleep(100);
    }
    if (ready) {
      return {
        port,
        url: "ws://127.0.0.1:" + port + "/ws",
        dump: () => dump,
        stop: async () => {
          try {
            child.kill("SIGTERM");
          } catch {
            /* 已经退了 */
          }
          const limit = Date.now() + 1500;
          while (!exited && Date.now() < limit) await sleep(50);
          if (!exited) {
            try {
              child.kill("SIGKILL");
            } catch {
              /* 收尾而已 */
            }
          }
        },
      };
    }
    lastDump = dump;
    try {
      child.kill("SIGKILL");
    } catch {
      /* 已经退了 */
    }
    await sleep(150);
  }
  throw new Error("通道起不来（换 5 个端口都没听上）：" + lastDump.split("\n").filter(Boolean).slice(-3).join(" / "));
};

// 群事件的另一种造法：换人、换话、可带 @（G 组要控说话人，fixture 只有一个人）。
const buildGroupEvent = (
  fixture: Record<string, unknown>,
  opts: { groupId: number; speaker: number; messageId: number; text: string; mention: boolean; selfId: number },
): Record<string, unknown> => {
  const event = buildEvent(fixture, {
    scene: "group",
    groupId: opts.groupId,
    userId: opts.speaker,
    messageId: opts.messageId,
    time: Math.floor(Date.now() / 1000),
  }) as Record<string, any>;
  const message: Array<Record<string, unknown>> = [];
  if (opts.mention) message.push({ type: "at", data: { qq: String(opts.selfId) } });
  message.push({ type: "text", data: { text: opts.text } });
  event.message = message;
  event.user_id = opts.speaker;
  event.sender = { user_id: opts.speaker, nickname: "测" + (opts.speaker % 1000), card: "测" + (opts.speaker % 1000), role: "member" };
  return event;
};

const runBatchSuite = async (config: Config): Promise<void> => {
  const fixture = JSON.parse(readFileSync(resolve(FIXTURES, "event.group.text.json"), "utf8"));
  const selfId = Number((fixture as Record<string, any>).self_id ?? 0);
  const policy = loadPolicy();
  const perGroupMax = policy.quota.perGroupMax;
  const defaultReplies = policy.outbound.defaultReplies;

  const core = await startFakeCore();
  let channel = await startChannel(core.url, "send");
  say("G 组：假核 " + core.url + "，通道 " + channel.url + "（mode=send）");
  ackEnabled = true;
  socket = await connect(channel.url);

  let messageSeq = 2_100_000_000 + (Date.now() % 200_000) * 10;
  let groupSeq = 0;
  const groupBase = 730_000_000 + (Date.now() % 100_000) * 100;
  const nextMessage = (): number => (messageSeq += 1);
  const nextGroup = (): number => {
    groupSeq += 1;
    return groupBase + groupSeq;
  };
  const post = (groupId: number, speaker: number, text: string, mention = false): number => {
    const messageId = nextMessage();
    socket.send(JSON.stringify(buildGroupEvent(fixture, { groupId, speaker, messageId, text, mention, selfId })));
    return messageId;
  };
  const hitsOf = (groupId: number): CoreHit[] => core.hits.filter((hit) => hit.body?.where?.groupId === groupId);
  const waitForHits = async (groupId: number, want: number, ms: number): Promise<CoreHit[]> => {
    const deadline = Date.now() + ms;
    while (hitsOf(groupId).length < want && Date.now() < deadline) await sleep(50);
    return hitsOf(groupId);
  };
  const speakersOf = (body: Record<string, any> | undefined): number[] => {
    const msgs = body?.batch?.msgs;
    return Array.isArray(msgs) ? msgs.map((item: any) => Number(item?.id)) : [];
  };
  const linesOf = (body: Record<string, any> | undefined): string[] =>
    typeof body?.text === "string" ? body.text.split("\n") : [];

  add("G0", "drill 自己起了假核 + 通道（独立端口，mode=send）", true, "核=" + core.url + " 通道=" + channel.url);

  try {
    // ---- G1：同一人连说三句（1.6s 间隔）→ 一段、一次核调用 ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      for (let i = 0; i < 3; i += 1) {
        post(groupId, SPEAKER_A, "同人第 " + (i + 1) + " 句");
        if (i < 2) await sleep(G1_GAP_MS);
      }
      const rows = await waitForDecisionRows(config.decisions, caseStart, 1, 12000);
      await sleep(500);
      const hits = hitsOf(groupId);
      const speakers = speakersOf(hits[0]?.body);
      add(
        "G1a",
        "同人 3 句（间隔 " + G1_GAP_MS + "ms）：只调核一次、决策账记 3 条",
        hits.length === 1 && rows[0]?.machine?.batch?.msgs === 3,
        "调核 " + hits.length + " 次，决策账 batch.msgs=" + String(rows[0]?.machine?.batch?.msgs),
      );
      add(
        "G1b",
        "段里 3 行、同一个 speaker",
        linesOf(hits[0]?.body).length === 3 && speakers.length === 3 && new Set(speakers).size === 1,
        "行数 " + linesOf(hits[0]?.body).length + " speakers=[" + speakers.join(",") + "]",
      );
    }

    // ---- G2：A/B 交替 6 条（0.3s）→ 一段 6 行、speaker 交替 ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      for (let i = 0; i < 6; i += 1) {
        post(groupId, i % 2 === 0 ? SPEAKER_A : SPEAKER_B, "交替第 " + (i + 1) + " 条");
        if (i < 5) await sleep(300);
      }
      await waitForDecisionRows(config.decisions, caseStart, 1, 10000);
      await sleep(500);
      const hits = hitsOf(groupId);
      const speakers = speakersOf(hits[0]?.body);
      const alternating = speakers.length === 6 && speakers.every((id, index) => id === (index % 2 === 0 ? SPEAKER_A : SPEAKER_B));
      add("G2a", "A/B 交替 6 条（间隔 0.3s）：只调核一次", hits.length === 1, "调核 " + hits.length + " 次");
      add(
        "G2b",
        "段里 6 行、speaker 交替",
        linesOf(hits[0]?.body).length === 6 && alternating,
        "行数 " + linesOf(hits[0]?.body).length + " speakers=[" + speakers.join(",") + "]",
      );
    }

    // ---- G3：A/B 交替 6 条（3s）→ 4 段，每段一次核调用 ----
    {
      // 安静档（quietOpen 2000ms）只在「上一条已经过去 >quietGapMs」时才开，而这个计时器是
      // 进程级、跨群的（batch.ts 的 lastArrivalAt）——刚跑完 G2 就接着发，第一条会按活跃档
      // （5000ms）开，整段变成 [2,2,2]。要验安静档就得先真等一个空档。
      const idle = policy.batch.quietGapMs + 1000;
      say("G3：先等 " + idle + "ms 让合批回到安静档（quietGapMs=" + policy.batch.quietGapMs + "）");
      await sleep(idle);
      const caseStart = Date.now();
      const groupId = nextGroup();
      const plan = [SPEAKER_A, SPEAKER_B, SPEAKER_A, SPEAKER_B, SPEAKER_A, SPEAKER_B];
      for (let i = 0; i < plan.length; i += 1) {
        post(groupId, plan[i], "G3-" + (i + 1));
        if (i < plan.length - 1) await sleep(3000);
      }
      const hits = await waitForHits(groupId, 4, 26000);
      await sleep(700);
      const all = hitsOf(groupId);
      const counts = all.map((hit) => (Array.isArray(hit.body?.batch?.msgs) ? hit.body.batch.msgs.length : -1));
      const firsts = all.map((hit) => Date.parse(String(hit.body?.batch?.at ?? "")));
      const offsets = firsts.map((value) => Math.round(value - firsts[0]));
      const wantOffsets = [0, 3000, 9000, 15000];
      const speakerPlan = all.map((hit) => speakersOf(hit.body).join("+")).join(" | ");
      const wantSpeakers = [String(SPEAKER_A), SPEAKER_B + "+" + SPEAKER_A, SPEAKER_B + "+" + SPEAKER_A, String(SPEAKER_B)].join(" | ");
      add("G3a", "间隔 3s 的 6 条 → 4 段（每段恰好一次核调用）", all.length === 4, "段数 " + all.length + "（核调用 " + hits.length + " 次）");
      add("G3b", "每段条数 = [1,2,2,1]", counts.join(",") === "1,2,2,1", "[" + counts.join(",") + "]");
      add(
        "G3c",
        "每段 firstAt 相对首段 = [0,3000,9000,15000]（±300ms）",
        offsets.length === 4 && wantOffsets.every((want, index) => Number.isFinite(offsets[index]) && Math.abs(offsets[index] - want) <= 300),
        "[" + offsets.join(",") + "]ms（按相对差比，不依赖机器时钟精度）",
      );
      add("G3d", "每段 speaker 序列 = A | B,A | B,A | B（段间不串人）", speakerPlan === wantSpeakers, speakerPlan);

      // 墙钟：batch.at / msgs[].at 现在都是真时间，能和决策行的 ts 直接对表（不再是 1970 年）。
      const stamps = decisionRowsSince(config.decisions, caseStart).map((row) => ({
        ts: Date.parse(String(row.ts)),
        at: Date.parse(String(row.machine?.batch?.at ?? "")),
      }));
      add(
        "G3e",
        "决策行 batch.at 是墙钟：ts ≥ at 且 ts−at ≤ hardCap(" + policy.batch.hardCapMs + ")+1s",
        stamps.length === 4 &&
          stamps.every(
            (item) =>
              Number.isFinite(item.ts) &&
              Number.isFinite(item.at) &&
              item.ts >= item.at &&
              item.ts - item.at <= policy.batch.hardCapMs + 1000,
          ),
        stamps.map((item) => "ts−at=" + (item.ts - item.at) + "ms").join(" ") || "没有决策行",
      );
      const monotonic = all.length === 4 && all.every((hit) => {
        const msgs = Array.isArray(hit.body?.batch?.msgs) ? hit.body.batch.msgs : [];
        const first = Date.parse(String(hit.body?.batch?.at ?? ""));
        const times = msgs.map((item: any) => Date.parse(String(item?.at)));
        return (
          times.length > 0 &&
          times.every(
            (time: number, index: number) => Number.isFinite(time) && time >= first && (index === 0 || time > times[index - 1]),
          )
        );
      });
      add(
        "G3f",
        "msgs[].at 严格递增，且都不早于段起点",
        monotonic,
        all
          .map((hit) => {
            const msgs = Array.isArray(hit.body?.batch?.msgs) ? hit.body.batch.msgs : [];
            const first = Date.parse(String(hit.body?.batch?.at ?? ""));
            return "[" + msgs.map((item: any) => Date.parse(String(item?.at)) - first).join(",") + "]";
          })
          .join(" "),
      );
    }

    // ---- G4：被 @ → 从消息到出手 ≤1000ms（+ 送核/下发余量） ----
    {
      const groupId = nextGroup();
      const start = mark();
      const sent = Date.now();
      post(groupId, SPEAKER_A, "叫你一下", true);
      const deadline = Date.now() + 5000;
      while (actionsSince(start).length === 0 && Date.now() < deadline) await sleep(20);
      const frames = framesSince(start);
      const index = frames.findIndex(isActionFrame);
      const latency = index >= 0 ? frameTimes[start + index] - sent : -1;
      const rows = await waitForDecisionRows(config.decisions, sent, 1, 5000);
      add(
        "G4",
        "@ 到出手 ≤1000ms（+ 送核/下发余量 200ms，等待上限本身是 mentionCapMs=1000）",
        latency >= 0 && latency <= 1200,
        "实测 " + latency + "ms；决策账 human=" + JSON.stringify(String(rows[rows.length - 1]?.human ?? "")),
      );
    }

    // ---- G5：1s×10 条 → 第一段 8s 硬顶出手，第 10 条落在第二段 ----
    {
      const groupId = nextGroup();
      const started = Date.now();
      for (let i = 0; i < 10; i += 1) {
        post(groupId, SPEAKER_A, "顶第 " + (i + 1) + " 条");
        if (i < 9) await sleep(1000);
      }
      await waitForHits(groupId, 2, 24000);
      await sleep(700);
      const all = hitsOf(groupId);
      const first = all[0]?.body?.batch;
      const second = all[1]?.body?.batch;
      const fire = all[0] === undefined ? -1 : all[0].t - started;
      const firstCount = Array.isArray(first?.msgs) ? first.msgs.length : -1;
      add(
        "G5a",
        "第一段在 8s 硬顶出手：出手−firstAt ≤8500、waited_ms ≤8000、条数 ≥8",
        first !== undefined && fire >= 0 && fire <= 8500 && Number(first.waited_ms) <= 8000 && firstCount >= 8,
        "出手 +" + fire + "ms waited=" + String(first?.waited_ms) + "ms 条数=" + firstCount,
      );
      const span =
        Array.isArray(first?.msgs) && first.msgs.length > 0
          ? Date.parse(String(first.msgs[first.msgs.length - 1].at)) - Date.parse(String(first.msgs[0].at))
          : -1;
      add("G5b", "第一段跨度 ≤8000ms（硬顶没被后面的消息延长）", span >= 0 && span <= 8000, "跨度 " + span + "ms");
      const between = first !== undefined && second !== undefined ? Date.parse(String(second.at)) - Date.parse(String(first.at)) : -1;
      add(
        "G5c",
        "第 10 条落在第二段（第二段 firstAt − 第一段 firstAt ≥ 8000ms）",
        between >= 8000,
        "两段 firstAt 差 " + between + "ms，第二段条数=" + (Array.isArray(second?.msgs) ? second.msgs.length : -1),
      );
    }

    // ---- G6：假核回 600 字 → 一条默认短句 + replaced_too_long ----
    {
      const groupId = nextGroup();
      const start = mark();
      const caseStart = Date.now();
      post(groupId, SPEAKER_A, "说长一点 " + LONG_MARK);
      const deadline = Date.now() + 6000;
      while (actionsSince(start).length === 0 && Date.now() < deadline) await sleep(50);
      await sleep(700);
      const actions = actionsSince(start);
      const text = typeof actions[0]?.params?.message?.[0]?.data?.text === "string" ? String(actions[0].params.message[0].data.text) : "";
      add(
        "G6a",
        "600 字回包 → 只发一条默认短句（≤" + policy.outbound.maxChars + " 字，取自 policy.defaultReplies）",
        actions.length === 1 && defaultReplies.includes(text) && Array.from(text).length <= policy.outbound.maxChars,
        "发出 " + actions.length + " 条，text=" + JSON.stringify(text) + " 长度=" + Array.from(text).length,
      );
      const rows = await waitForDecisionRows(config.decisions, caseStart, 1, 5000);
      const last = rows[rows.length - 1];
      add(
        "G6b",
        "决策账 replaced_too_long（gates 里 limits 命中）",
        last?.machine?.reason_code === "replaced_too_long" &&
          (last?.machine?.gates ?? []).some((gate) => gate.name === "limits" && gate.hit === true),
        "reason_code=" + String(last?.machine?.reason_code) + " gates=" + JSON.stringify(last?.machine?.gates ?? []),
      );
    }

    // ---- G9：send:false（主动沉默）→ 0 action、账上 1 行 silent、决策 core_empty + 理由 ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      const start = mark();
      const messageId = post(groupId, SPEAKER_A, "不想说就直说 " + SILENT_MARK);
      const rows = await waitForDecisionRows(config.decisions, caseStart, 1, 8000);
      await sleep(1200);
      const actions = actionsSince(start);
      const mine = rowsFor(readRowsFile(config.ledger), "mid:" + messageId);
      const silentRows = mine.filter((row) => row.kind === "silent");
      const decision = rows[rows.length - 1];
      add("G9a", "send:false（主动沉默）：0 个 action 帧", actions.length === 0, "收到 " + actions.length + " 个 action 帧");
      add(
        "G9b",
        "账里该 messageId 恰好 1 行 silent，note 含核给的理由",
        silentRows.length === 1 && String(silentRows[0]?.note ?? "").includes("刚在忙"),
        "silent=" + silentRows.length + " 行 note=" + JSON.stringify(String(silentRows[0]?.note ?? "")),
      );
      const whyText = String(decision?.machine?.why ?? "");
      add(
        "G9c",
        "决策账 1 行：reason_code=core_empty、decided=silent、reason 含理由且理由不进 why",
        rows.length === 1 &&
          decision?.machine?.reason_code === "core_empty" &&
          decision?.machine?.decided === "silent" &&
          String(decision?.machine?.reason ?? "").includes("刚在忙") &&
          !whyText.includes("刚在忙"),
        "行数 " + rows.length + " reason_code=" + String(decision?.machine?.reason_code) +
          " decided=" + String(decision?.machine?.decided) + " reason=" + JSON.stringify(String(decision?.machine?.reason ?? "")) +
          " why=" + JSON.stringify(whyText),
      );
      add("G9d", "核只被调 1 次（没有重试）", hitsOf(groupId).length === 1, "调核 " + hitsOf(groupId).length + " 次");
    }

    // ---- G10 对照：chunks: [] 但**没有** send 字段 → 仍然是原来的 empty ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      const start = mark();
      const messageId = post(groupId, SPEAKER_A, "憋回去了 " + EMPTY_MARK);
      const rows = await waitForDecisionRows(config.decisions, caseStart, 1, 8000);
      await sleep(900);
      const mine = rowsFor(readRowsFile(config.ledger), "mid:" + messageId);
      const emptyRows = mine.filter((row) => row.kind === "empty");
      const silentRows = mine.filter((row) => row.kind === "silent");
      const actions = actionsSince(start);
      const decision = rows[rows.length - 1];
      add(
        "G10a",
        "没有 send 字段的空 chunks → 账里 1 行 empty、0 行 silent、0 个 action",
        emptyRows.length === 1 && silentRows.length === 0 && actions.length === 0,
        "empty=" + emptyRows.length + " silent=" + silentRows.length + " action=" + actions.length,
      );
      add(
        "G10b",
        "决策账 core_empty、decided=silent",
        decision?.machine?.reason_code === "core_empty" && decision?.machine?.decided === "silent",
        "reason_code=" + String(decision?.machine?.reason_code) + " decided=" + String(decision?.machine?.decided) +
          " why=" + JSON.stringify(String(decision?.machine?.why ?? "")),
      );
    }

    // ---- G8：同群连发超过配额上限 → 第 N+1 段不出手、quota_exceeded ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      const start = mark();
      const segments = perGroupMax + 1;
      for (let index = 0; index < segments; index += 1) {
        post(groupId, SPEAKER_A, "配额" + (index + 1) + "-a");
        await sleep(120);
        post(groupId, SPEAKER_B, "配额" + (index + 1) + "-b");
        await waitForDecisionRows(config.decisions, caseStart, index + 1, 6000);
        await sleep(150);
      }
      await sleep(500);
      const actions = actionsSince(start);
      const hits = hitsOf(groupId);
      const rows = decisionRowsSince(config.decisions, caseStart);
      add(
        "G8a",
        "前 " + perGroupMax + " 段各回一条，第 " + segments + " 段不出手",
        actions.length === perGroupMax && rows.length >= segments,
        "action " + actions.length + " 个，决策 " + rows.length + " 行",
      );
      add(
        "G8b",
        "决策账最后一行 quota_exceeded",
        rows[rows.length - 1]?.machine?.reason_code === "quota_exceeded",
        "reason_code=" + String(rows[rows.length - 1]?.machine?.reason_code) + " human=" + JSON.stringify(String(rows[rows.length - 1]?.human ?? "")),
      );
      add("G8c", "第 " + segments + " 段没调核（调核次数 = " + perGroupMax + "）", hits.length === perGroupMax, "调核 " + hits.length + " 次");
    }

    // ---- G11：refund——主动沉默/空回不占配额；对照组 6 次真出手后第 7 次被挡 ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      // 每段 A+B（换人档 500ms 就到期），6 段主动沉默：refund 之后配额该一直是 0 条。
      for (let index = 0; index < 6; index += 1) {
        post(groupId, SPEAKER_A, "沉默" + (index + 1) + "-a " + SILENT_MARK);
        await sleep(100);
        post(groupId, SPEAKER_B, "沉默" + (index + 1) + "-b");
        await waitForDecisionRows(config.decisions, caseStart, index + 1, 8000);
        await sleep(120);
      }
      const afterSilent = quotaStamps(groupId);
      add("G11a", "6 段 send:false 之后该群配额仍是 0 条（refund 生效）", afterSilent.length === 0, "配额 " + afterSilent.length + " 条");

      // 第 7 段正常回包：配额没被白占，必须真的出手。
      const startSeventh = mark();
      post(groupId, SPEAKER_A, "第七段该说话了-a");
      await sleep(100);
      post(groupId, SPEAKER_B, "第七段该说话了-b");
      await waitForDecisionRows(config.decisions, caseStart, 7, 8000);
      await sleep(400);
      const seventhActions = actionsSince(startSeventh).length;
      const afterSeventh = quotaStamps(groupId);
      add(
        "G11b",
        "第 7 段真的出手（配额只被它自己占了一次）",
        seventhActions === 1 && afterSeventh.length === 1,
        "action " + seventhActions + " 个，配额 " + afterSeventh.length + " 条",
      );

      // 对照组：这一群接着连打 5 段正常回包（累计 6 次），第 7 次必须被配额挡住。
      let want = decisionRowsSince(config.decisions, caseStart).length;
      for (let index = 0; index < 5; index += 1) {
        post(groupId, SPEAKER_A, "对照" + (index + 1) + "-a");
        await sleep(100);
        post(groupId, SPEAKER_B, "对照" + (index + 1) + "-b");
        want += 1;
        await waitForDecisionRows(config.decisions, caseStart, want, 8000);
        await sleep(120);
      }
      const beforeRefusal = mark();
      post(groupId, SPEAKER_A, "超额这一段-a");
      await sleep(100);
      post(groupId, SPEAKER_B, "超额这一段-b");
      want += 1;
      const rows = await waitForDecisionRows(config.decisions, caseStart, want, 8000);
      await sleep(400);
      const refusedActions = actionsSince(beforeRefusal).length;
      const lastRow = rows[rows.length - 1];
      add(
        "G11c",
        "对照组：6 次真出手之后第 7 次 quota_exceeded 且不出手",
        refusedActions === 0 && lastRow?.machine?.reason_code === "quota_exceeded",
        "action " + refusedActions + " 个，末行 " + String(lastRow?.machine?.reason_code) + "，配额 " + quotaStamps(groupId).length + " 条",
      );
    }

    // ---- G13：出站全败（协议端不回 ack）→ 配额退还 ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      ackEnabled = false; // 不回 ack：第一泡会 ack 超时，第二泡记 unsent，这一轮 sent=0
      const messageId = post(groupId, SPEAKER_A, "这条发不出去 " + TWO_MARK);
      const rows = await waitForDecisionRows(config.decisions, caseStart, 1, 30000);
      await sleep(600);
      const mine = rowsFor(readRowsFile(config.ledger), "mid:" + messageId);
      const outboundFail = mine.filter((row) => row.kind === "uncertain" || row.kind === "failed" || row.kind === "unsent");
      const decision = rows[rows.length - 1];
      const stamps = quotaStamps(groupId);
      add("G13a", "出站全败（sent=0）后该群配额仍是 0 条（退还生效）", stamps.length === 0, "配额 " + stamps.length + " 条");
      add(
        "G13b",
        "ledger 里有 uncertain/failed/unsent 行（真的走到出站又失败了）",
        outboundFail.length >= 1,
        "行数 " + outboundFail.length + " kinds=" + JSON.stringify(outboundFail.map((row) => row.kind)) +
          " 首个 note=" + JSON.stringify(String(outboundFail[0]?.note ?? "")),
      );
      add(
        "G13c",
        "决策账一行：reason_code=outbound_failed、decided=silent、human 说「没确认」、why 说 outbound",
        rows.length === 1 &&
          decision?.machine?.decided === "silent" &&
          decision?.machine?.reason_code === "outbound_failed" &&
          decision?.human === "没回：发出去了但没确认（协议端没回执）" &&
          String(decision?.machine?.why ?? "").includes("outbound"),
        "行数 " + rows.length + " decided=" + String(decision?.machine?.decided) +
          " reason_code=" + String(decision?.machine?.reason_code) + " human=" + JSON.stringify(String(decision?.human ?? "")) +
          " why=" + JSON.stringify(String(decision?.machine?.why ?? "")),
      );

      // 对照组：正常 ack 的那一段 → 配额 1 条，不退。
      ackEnabled = true;
      const controlGroup = nextGroup();
      const controlStart = Date.now();
      const controlMark = mark();
      post(controlGroup, SPEAKER_A, "这条发得出去");
      await waitForDecisionRows(config.decisions, controlStart, 1, 12000);
      await sleep(400);
      const controlActions = actionsSince(controlMark).length;
      add(
        "G13d",
        "对照组：正常 ack 的那段配额 1 条（出手过就不退）",
        controlActions === 1 && quotaStamps(controlGroup).length === 1,
        "action " + controlActions + " 个，配额 " + quotaStamps(controlGroup).length + " 条",
      );
    }

    // ---- G7：silent → 一条 action 都不出，决策账 silent_mode + 账里 blocked ----
    {
      await channel.stop();
      channel = await startChannel(core.url, "silent");
      ackEnabled = false;
      try {
        socket.close();
      } catch {
        /* 收尾而已 */
      }
      socket = await connect(channel.url);
      say("G7：换成 silent 通道 " + channel.url);
      const groupId = nextGroup();
      const caseStart = Date.now();
      const start = mark();
      const messageId = post(groupId, SPEAKER_A, "silent 也别说");
      const rows = await waitForDecisionRows(config.decisions, caseStart, 1, 8000);
      await sleep(Math.min(3000, config.wait));
      const actions = actionsSince(start);
      add("G7a", "silent：一条 action 都不出", actions.length === 0, "收到 " + actions.length + " 个 action 帧");
      add(
        "G7b",
        "决策账有一行 silent_mode",
        rows.some((row) => row.machine?.reason_code === "silent_mode"),
        "reason_code=[" + rows.map((row) => String(row.machine?.reason_code)).join(",") + "]",
      );
      const blockedRows = rowsFor(readRowsFile(config.ledger), "mid:" + messageId).filter((row) => row.kind === "blocked");
      add(
        "G7c",
        "账里有 blocked 行，且 text 非空（她本来要说什么）",
        blockedRows.length === 1 && String(blockedRows[0]?.text ?? "").length > 0 && /chunks=\d+/.test(String(blockedRows[0]?.note ?? "")),
        "blocked=" + blockedRows.length + " text=" + JSON.stringify(String(blockedRows[0]?.text ?? "")) +
          " note=" + JSON.stringify(String(blockedRows[0]?.note ?? "")),
      );
    }

    // ---- G12：silent 仍然调核（门是唯一变量）、仍然不扣配额 ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      const start = mark();
      const messageId = post(groupId, SPEAKER_A, "silent 也走核这趟");
      const rows = await waitForDecisionRows(config.decisions, caseStart, 1, 8000);
      await sleep(1200);
      const hits = hitsOf(groupId);
      const actionCount = actionsSince(start).length;
      const blockedRows = rowsFor(readRowsFile(config.ledger), "mid:" + messageId).filter((row) => row.kind === "blocked");
      const stamps = quotaStamps(groupId);
      const lastRow = rows[rows.length - 1];
      add("G12a", "silent 仍然调核 1 次（门是唯一变量）", hits.length === 1, "调核 " + hits.length + " 次");
      add("G12b", "silent：0 个 action 帧", actionCount === 0, "收到 " + actionCount + " 个 action 帧");
      add(
        "G12c",
        "blocked 行 text 非空、note 带 chunks=（她本来要说什么）",
        blockedRows.length === 1 && String(blockedRows[0]?.text ?? "").length > 0 && /chunks=\d+/.test(String(blockedRows[0]?.note ?? "")),
        "blocked=" + blockedRows.length + " text=" + JSON.stringify(String(blockedRows[0]?.text ?? "")) +
          " note=" + JSON.stringify(String(blockedRows[0]?.note ?? "")),
      );
      add(
        "G12d",
        "决策账 silent_mode，且该群配额没有新增时间戳（silent 不扣）",
        lastRow?.machine?.reason_code === "silent_mode" && stamps.length === 0,
        "reason_code=" + String(lastRow?.machine?.reason_code) + " 配额 " + stamps.length + " 条",
      );
    }
  } finally {
    await channel.stop();
    await core.close();
  }

  const allPass = printTable();
  process.exit(allPass ? 0 : 1);
};

const main = async (): Promise<void> => {
  const config = parseConfig(process.argv.slice(2));
  if (config.help) {
    printHelp();
    process.exit(0);
  }
  failRetcode = config.failRetcode;
  ackEnabled = config.expectMode === "send";

  // G 组自成一体：自己起假核 + 通道，不碰 --url 上那个（也不读它的模式）。
  if (config.batch) {
    await runBatchSuite(config);
    return;
  }

  const fixtureName = config.scene === "group" ? "event.group.text.json" : "event.private.text.json";
  const fixture = JSON.parse(readFileSync(resolve(FIXTURES, fixtureName), "utf8"));
  const messageId = 2_000_000_000 + ((Date.now() % 1_000_000) * 100 + (process.pid % 100));
  const event = buildEvent(fixture, {
    scene: config.scene,
    groupId: config.groupId,
    userId: config.userId,
    messageId,
    time: Math.floor(Date.now() / 1000),
  });
  const key = "mid:" + messageId;
  const targetId = config.scene === "group" ? config.groupId : config.userId;
  const expect: Expect = { scene: config.scene, targetId };

  say("起步：mode=" + config.expectMode + " scene=" + config.scene + " target=" + targetId + " messageId=" + messageId + " key=" + key);
  say("地址=" + config.url + " 窗口=" + config.wait + "ms 账=" + (isAbsolute(config.ledger) ? config.ledger : resolve(config.ledger)));
  say("fail-retcode=" + String(config.failRetcode) + " reconnect=" + String(config.reconnect) + " once=" + String(config.once));

  try {
    socket = await connect(config.url);
  } catch (error) {
    say("连不上 " + config.url + "：" + (error as Error).message + "（通道没起？）");
    process.exitCode = 1;
    return;
  }
  say("已连上 " + config.url);

  // 账里同 key 的旧行会干扰断言：这次的 message_id 是新造的，正常情况下应为 0 行。
  // 同时留一份「开跑时的账」给断言 E：那里面的 key 一定是本次运行之前就有的。
  const ledgerAtStart = readRowsFile(config.ledger);
  const preexisting = rowsFor(ledgerAtStart, key).length;
  if (preexisting > 0) say("注意：账里已有 " + preexisting + " 行同 key 记录（message_id 撞了？断言会失真）");

  if (config.expectMode === "silent") {
    const start = mark();
    socket.send(JSON.stringify(event));
    say("投递 #1（silent 窗口 " + config.wait + "ms，等一个什么都别发生）");
    await sleep(config.wait);

    const actions = actionsSince(start);
    add(
      "A1",
      "silent：窗口内 0 个 action 帧",
      actions.length === 0,
      actions.length === 0 ? "收到 0 个 action 帧" : "收到 " + actions.length + " 个：" + JSON.stringify(actions[0]).slice(0, 160),
    );

    let rows = await waitForRows(config.ledger, (all) => countKindFor(all, key, "blocked") >= 1, Math.max(2000, Math.min(config.wait, 8000)));
    const blocked = rowsFor(rows, key).filter((row) => row.kind === "blocked");
    const emptyRows = rowsFor(rows, key).filter((row) => row.kind === "empty");
    add(
      "A2",
      "账里有该 messageId 的 kind=blocked 行",
      blocked.length >= 1,
      blocked.length > 0
        ? "blocked=" + blocked.length + " 行，末行 " + describeRow(blocked[blocked.length - 1])
        : emptyRows.length > 0
          ? "账里是 kind=empty（核这轮没回泡），演练需要核至少回一个泡"
          : "账里该 key 共 " + rowsFor(rows, key).length + " 行",
    );
    const lastBlocked = blocked[blocked.length - 1];
    add(
      "A3",
      "blocked 行能回答「想发什么」（text 非空、note 带 chunks/send_at）",
      Boolean(lastBlocked && typeof lastBlocked.text === "string" && lastBlocked.text.length > 0 && /chunks=\d+/.test(String(lastBlocked.note ?? ""))),
      lastBlocked ? "text=" + JSON.stringify(String(lastBlocked.text).slice(0, 40)) + " note=" + JSON.stringify(String(lastBlocked.note ?? "")) : "没有 blocked 行",
    );
  } else {
    const start = mark();
    socket.send(JSON.stringify(event));
    say("投递 #1（send 窗口 " + config.wait + "ms，边收边应答）");
    await sleep(config.wait);

    const actions = actionsSince(start);
    const roundRows = rowsFor(readRowsFile(config.ledger), key);
    const roundEmpty = countKind(roundRows, "empty");
    const roundError = countKind(roundRows, "error");
    add(
      "B1",
      "send：窗口内收到 N>=1 个 action 帧",
      actions.length >= 1,
      actions.length > 0
        ? "收到 " + actions.length + " 个"
        : "收到 0 个；账里 empty=" + roundEmpty + " error=" + roundError +
          (roundError > 0 ? "（" + String(roundRows.filter((row) => row.kind === "error").pop()?.note ?? "") + "）" : ""),
    );

    const errors: string[] = [];
    actions.forEach((frame, index) => {
      for (const message of validateAction(frame, expect)) errors.push("#" + index + " " + message);
    });
    // 没有帧时不算「全部合格」：空集合套断言会造出假 PASS。
    add(
      "B2",
      "每个 action 的动作名/目标/message 段/echo 形态都对",
      actions.length > 0 && errors.length === 0,
      actions.length === 0 ? "没有 action 帧可校验" : errors.length === 0 ? "全部合格" : errors.slice(0, 3).join("；"),
    );

    const echoes = actions.map((frame) => String(frame?.echo));
    const sequenceErrors = actions.length > 0 ? checkEchoSequence(echoes) : ["没有 action 帧，无从校验 echo"];
    add("B3", "echo 是 req-<批号>-<泡序号>，泡序号从 0 严格递增", sequenceErrors.length === 0, sequenceErrors.length === 0 ? echoes.join(", ") : sequenceErrors.slice(0, 3).join("；"));

    if (config.failRetcode === null) {
      const rows = await waitForRows(
        config.ledger,
        (all) => (actions.length === 0 ? false : countKindFor(all, key, "sent") >= actions.length),
        Math.max(2000, Math.min(config.wait, 8000)),
      );
      const sent = rowsFor(rows, key).filter((row) => row.kind === "sent");
      add("B4", "账里每泡一行 kind=sent，共 " + actions.length + " 行", sent.length === actions.length && actions.length > 0, "sent=" + sent.length + " 行");
      const badRetcode = sent.filter((row) => row.retcode !== 0);
      const indexWrong = sent.map((row) => row.index).join(",") !== sent.map((_row, index) => index).join(",");
      add(
        "B5",
        "sent 行 retcode=0，且 index 覆盖 0..N-1",
        sent.length > 0 && badRetcode.length === 0 && !indexWrong,
        "retcode=[" + sent.map((row) => String(row.retcode)).join(",") + "] index=[" + sent.map((row) => String(row.index)).join(",") + "]",
      );
    } else {
      const rows = await waitForRows(config.ledger, (all) => countKindFor(all, key, "failed") >= 1, Math.max(2000, Math.min(config.wait, 8000)));
      const failed = rowsFor(rows, key).filter((row) => row.kind === "failed");
      add("B4", "收到失败 ack 后账里有 kind=failed 行", failed.length >= 1, failed.length > 0 ? "failed=" + failed.length + " 行，末行 " + describeRow(failed[failed.length - 1]) : "没有 failed 行");
      const matched = failed.filter((row) => row.retcode === config.failRetcode);
      add("B5", "failed 行保留原始 retcode=" + config.failRetcode, matched.length >= 1, "retcode=[" + failed.map((row) => String(row.retcode)).join(",") + "]");
      add("B6", "失败后不再发后面的泡（只收到 1 个 action 帧）", actions.length === 1, "收到 " + actions.length + " 个 action 帧");
      add("B7", "失败不降级记录为 sent", countKindFor(rows, key, "sent") === 0, "sent=" + countKindFor(rows, key, "sent") + " 行");
    }
  }

  if (!config.once) {
    // ---- 断言 C：同一条事件（同 message_id）再投一次，必须被去重挡在调核之前 ----
    const beforeRows = rowsFor(readRowsFile(config.ledger), key);
    const beforeDup = countKind(beforeRows, "dup");
    const beforeSent = countKind(beforeRows, "sent");
    const beforeTotal = beforeRows.length;
    const start = mark();
    socket.send(JSON.stringify(event));
    say("投递 #2（同 messageId，验去重）");
    await sleep(config.wait);

    const actions = actionsSince(start);
    add("C1", "重投同一 messageId：窗口内 0 个新 action 帧", actions.length === 0, "收到 " + actions.length + " 个");

    const rows = await waitForRows(config.ledger, (all) => countKindFor(all, key, "dup") >= beforeDup + 1, Math.max(2000, Math.min(config.wait, 8000)));
    const afterRows = rowsFor(rows, key);
    const afterDup = countKind(afterRows, "dup");
    const dupRow = afterRows.filter((row) => row.kind === "dup").pop();
    add("C2", "账里多一条 kind=dup", afterDup === beforeDup + 1, "dup " + beforeDup + " → " + afterDup + "，" + describeRow(dupRow));
    add("C3", "sent 行数没有增加", countKind(afterRows, "sent") === beforeSent, "sent " + beforeSent + " → " + countKind(afterRows, "sent"));
    add("C4", "该 key 只多 1 行账（不多记也不漏记）", afterRows.length === beforeTotal + 1, "行数 " + beforeTotal + " → " + afterRows.length);

    // ---- 断言 D：断开重连后第三次投递，去重记忆不能随连接丢 ----
    if (config.reconnect) {
      say("--reconnect：断开，1s 后重连");
      try {
        socket.close();
      } catch {
        /* 已经关了 */
      }
      await sleep(1000);
      try {
        socket = await connect(config.url);
        add("D1", "断开 1s 后重连成功", true, "已连上 " + config.url);
      } catch (error) {
        add("D1", "断开 1s 后重连成功", false, "重连失败：" + (error as Error).message);
      }

      const beforeReconnectRows = rowsFor(readRowsFile(config.ledger), key);
      const beforeReconnectDup = countKind(beforeReconnectRows, "dup");
      const beforeReconnectSent = countKind(beforeReconnectRows, "sent");
      const start2 = mark();
      socket.send(JSON.stringify(event));
      say("投递 #3（重连后同 messageId，验去重不随连接丢）");
      await sleep(config.wait);

      const actions2 = actionsSince(start2);
      add("D2", "重连后第三次投递：0 个新 action 帧", actions2.length === 0, "收到 " + actions2.length + " 个");

      const rows2 = await waitForRows(config.ledger, (all) => countKindFor(all, key, "dup") >= beforeReconnectDup + 1, Math.max(2000, Math.min(config.wait, 8000)));
      const afterReconnectRows = rowsFor(rows2, key);
      add("D3", "账里 dup 再 +1", countKind(afterReconnectRows, "dup") === beforeReconnectDup + 1, "dup " + beforeReconnectDup + " → " + countKind(afterReconnectRows, "dup"));
      add("D4", "重连后 sent 行数仍没有增加", countKind(afterReconnectRows, "sent") === beforeReconnectSent, "sent " + beforeReconnectSent + " → " + countKind(afterReconnectRows, "sent"));
    }
  }

  if (!config.once) {
    // ---- 断言 E：账里「本次运行之前」就存在的 key，重投必须照样被挡住 ----
    // 去重集合是进程启动时从账里全量读的，所以哪怕通道重启过，旧 key 也认得出；
    // 这里不杀别人的进程，改从盘上挑一个更早的 key 来验「认盘不认内存」。
    const oldKeys = Array.from(new Set(ledgerAtStart.map((row) => row.key))).filter(
      (candidate): candidate is string => typeof candidate === "string" && /^mid:-?[0-9]+$/.test(candidate) && candidate !== key,
    );
    const replayKey = oldKeys[0] ?? key;
    const replayMessageId = Number(replayKey.slice(4));
    const beforeReplay = rowsFor(readRowsFile(config.ledger), replayKey);
    const beforeReplayDup = countKind(beforeReplay, "dup");
    const beforeReplayFlowed = beforeReplay.filter((row) => ["sent", "failed", "uncertain", "unsent"].includes(String(row.kind))).length;
    const beforeReplayTotal = beforeReplay.length;
    const startReplay = mark();
    socket.send(
      JSON.stringify(
        buildEvent(fixture, {
          scene: config.scene,
          groupId: config.groupId,
          userId: config.userId,
          messageId: replayMessageId,
          time: Math.floor(Date.now() / 1000),
        }),
      ),
    );
    say("投递 #4（重投账里的旧 key=" + replayKey + "，验去重认盘不认内存）");
    await sleep(config.wait);

    const replayActions = actionsSince(startReplay);
    add(
      "E1",
      "重投账里早已存在的 key：窗口内 0 个新 action 帧",
      replayActions.length === 0,
      "收到 " + replayActions.length + " 个" + (oldKeys.length === 0 ? "（本次运行前账里没有更早的 key，只能退用本次 key）" : ""),
    );

    const rowsE = await waitForRows(config.ledger, (all) => countKindFor(all, replayKey, "dup") >= beforeReplayDup + 1, Math.max(2000, Math.min(config.wait, 8000)));
    const afterReplay = rowsFor(rowsE, replayKey);
    add("E2", "该旧 key 的 dup 再 +1", countKind(afterReplay, "dup") === beforeReplayDup + 1, "dup " + beforeReplayDup + " → " + countKind(afterReplay, "dup"));
    const afterReplayFlowed = afterReplay.filter((row) => ["sent", "failed", "uncertain", "unsent"].includes(String(row.kind))).length;
    add("E3", "旧 key 的 sent/failed/uncertain/unsent 行数不变（没有重发一遍）", afterReplayFlowed === beforeReplayFlowed, "发送类行数 " + beforeReplayFlowed + " → " + afterReplayFlowed);
    add("E4", "该旧 key 只多 1 行账", afterReplay.length === beforeReplayTotal + 1, "行数 " + beforeReplayTotal + " → " + afterReplay.length);
  }

  if (!config.once && config.expectMode === "send" && config.inflight) {
    // ---- 断言 F：在飞窗口重投（第一批还没发完就重投） ----
    // 去重的第一道是「认领」：调核之前一次同步的内存占位，占位不落任何账行。
    // 若认领退回成「查账」，等 ack 的这段窗口里 key 在账上是隐形的，同一条会被领两次、发两遍。
    let inflightId = messageId + 1;
    while (ledgerAtStart.some((row) => row.key === "mid:" + inflightId)) inflightId += 1;
    const inflightKey = "mid:" + inflightId;
    const inflightEvent = buildEvent(fixture, {
      scene: config.scene,
      groupId: config.groupId,
      userId: config.userId,
      messageId: inflightId,
      time: Math.floor(Date.now() / 1000),
    });
    const startF = mark();
    inflightReplayed = false;
    inflightReplay = inflightEvent;
    ackDelayMs = 900;
    socket.send(JSON.stringify(inflightEvent));
    say("投递 #5（在飞窗口重投：第一帧一到就再投同一条，ack 每帧延后 900ms）");
    await sleep(Math.max(config.wait, 6000));
    ackDelayMs = 0;
    inflightReplay = null;

    const acts = actionsSince(startF);
    const byReq = new Map<number, number[]>();
    for (const frame of acts) {
      const parts = echoParts(String(frame?.echo));
      if (parts === null) continue;
      const list = byReq.get(parts.req) ?? [];
      list.push(parts.index);
      byReq.set(parts.req, list);
    }
    const batchSize = Math.max(0, ...Array.from(byReq.values()).map((list) => list.length));
    const echoes = acts.map((frame) => String(frame?.echo)).join(",");

    add(
      "F1",
      "在飞重投后：action 总数 == 该批泡数 N（不是 2N）",
      acts.length > 0 && byReq.size === 1 && acts.length === batchSize,
      "收到 " + acts.length + " 个 action，请求号 " + byReq.size + " 个，N=" + batchSize + "｜" + echoes.slice(0, 120),
    );

    const flowedKinds = ["sent", "failed", "uncertain", "unsent"];
    const flowedOf = (rows: LedgerRow[]): number => rows.filter((row) => flowedKinds.includes(String(row.kind))).length;
    const rowsF = await waitForRows(
      config.ledger,
      (all) => countKindFor(all, inflightKey, "dup") >= 1 && flowedOf(rowsFor(all, inflightKey)) >= 1,
      Math.max(2000, Math.min(config.wait, 8000)),
    );
    const inflightRows = rowsFor(rowsF, inflightKey);

    add(
      "F2",
      "账里该 key 的发送类行数 == N（没有翻倍）",
      batchSize > 0 && flowedOf(inflightRows) === batchSize,
      "发送类 " + flowedOf(inflightRows) + " 行，N=" + batchSize,
    );

    const dups = inflightRows.filter((row) => row.kind === "dup");
    const dupNote = String(dups[0]?.note ?? "");
    // M4 起 batched 行在入段时就落地，所以重投看到的是「已有 kind=batched」；
    // 两种 note 都说明去重生效（一个是账里已有真行，一个是还在飞），都算过。
    add(
      "F3",
      "账里该 key 恰好多 1 行 dup，note 说清是「已有 kind=…」还是「在飞」",
      dups.length === 1 && (dupNote.includes("已有 kind=") || dupNote.includes("在飞")),
      "dup=" + dups.length + " note=" + JSON.stringify(dupNote),
    );

    if (config.channelLog !== null) {
      const text = existsSync(config.channelLog) ? readFileSync(config.channelLog, "utf8") : "";
      const reqId = byReq.size > 0 ? Array.from(byReq.keys())[0] : -1;
      let planLines = 0;
      for (const line of text.split("\n")) {
        const matched = /(?:^|\s)plan\s+req#([0-9]+)\s/.exec(line);
        if (matched !== null && Number(matched[1]) === reqId) planLines += 1;
      }
      add("F4", "通道日志里这批只有 1 行 plan", planLines === 1, "plan 行 " + planLines + " 行（req#" + reqId + "）");
    } else {
      // 没有通道日志时退一步：两个 plan 必然对应两组不同的请求号，请求号唯一就等于只开了一个 plan。
      add("F4", "只有一个 plan（未给 --channel-log，按 echo 请求号唯一反推）", byReq.size === 1, "不同请求号 " + byReq.size + " 个");
    }
  } else if (!config.once && config.expectMode === "silent") {
    say("silent 模式不跑断言 F（在飞窗口要 action 帧才触发）");
  }

  const allPass = printTable();
  try {
    socket.close();
  } catch {
    /* 收尾而已 */
  }
  process.exit(allPass ? 0 : 1);
};

main().catch((error) => {
  say("演练中断：" + (error as Error).message);
  process.exit(1);
});
