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
import { readFileSync, existsSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer as createHttpServer } from "node:http";
import { connect as netDial, createServer as createNetServer } from "node:net";
import { loadPolicy } from "../policy.ts";
import { loadGateWords } from "../direct.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(HERE, "..", "fixtures");
// M8 隔离：演练的默认值全部指向自己的端口与自己的状态目录。
// 生产端口与生产账本目录演练一个字节都不许碰（这条由 hygiene.test.ts 守门）。
const DRILL_PORT = 6299;
const DRILL_STATE = "/tmp/ch-drill";
const DRILL_URL = "ws://127.0.0.1:" + DRILL_PORT + "/ws";

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
  // G14 组（POST /say 主动开口）：同样自成一体，默认不跑，免得影响既有 40 条节奏。
  say: boolean;
  // 演练自己的状态目录（账 / 决策账 / 配额 / own 全在里面）。默认 /tmp/ch-drill。
  state: string;
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
  // 状态目录先定：ledger / decisions 的默认值都挂在它下面。
  const state = raw(argv, "state") ?? DRILL_STATE;
  const expectRaw = raw(argv, "expect-mode") ?? "silent";
  const expectMode = expectRaw === "send" ? "send" : "silent";
  const privateMode = has(argv, "private");
  const failRetcodeRaw = raw(argv, "fail-retcode");
  const failRetcode = failRetcodeRaw === undefined ? null : Number(failRetcodeRaw);
  return {
    url: raw(argv, "url") ?? DRILL_URL,
    expectMode,
    scene: privateMode ? "private" : "group",
    groupId: Number(raw(argv, "group") ?? String(freshGroupId())),
    userId: Number(raw(argv, "user") ?? "3000000003"),
    wait: Number(raw(argv, "wait") ?? "8000"),
    failRetcode: failRetcode !== null && Number.isFinite(failRetcode) ? failRetcode : null,
    reconnect: has(argv, "reconnect"),
    ledger: raw(argv, "ledger") ?? join(state, "ledger.jsonl"),
    once: has(argv, "once"),
    // 断言 F 默认跑；--no-inflight 关掉（不想让 drill 在通道里制造在飞并发时用）。
    inflight: !has(argv, "no-inflight"),
    // 断言 F4 想真数 plan 行，就把通道的 stdout 文件指过来；不给就按 echo 请求号反推。
    channelLog: raw(argv, "channel-log") ?? null,
    // G 组（合批）自成一体：自己起假核 + 通道，不碰 --url 上那个。
    batch: has(argv, "batch"),
    say: has(argv, "say"),
    state,
    decisions: raw(argv, "decisions") ?? join(state, "decisions.jsonl"),
    help: has(argv, "help"),
  };
};

export const printHelp = (): void => {
  const lines = [
    "drill.ts —— 扮演协议端，对运行中的 OneBot 通道下断言（不出站 / 分泡与 ack / 去重 / 重连去重）",
    "",
    "参数：",
    "  --expect-mode silent|send  通道应有的模式（决定跑哪组断言），默认 silent",
    "  --url <ws url>             通道地址，默认 ws://127.0.0.1:6299/ws（演练专用端口）",
    "  --state <dir>              演练自己的状态目录，默认 /tmp/ch-drill（账/决策账/配额/own 都在里面）",
    "  --group <id>               群号（群消息的目标），默认每次新造（M4 有每群 1 小时配额）",
    "  --user <id>                私聊对象 QQ 号，默认 3000000003",
    "  --private                  改跑私聊事件（用的是 fixtures/event.private.text.json 的结构）",
    "  --wait <ms>                每个观察窗口的毫秒数，默认 8000；窗口内不早退",
    "  --fail-retcode <n>         收到 action 就回 retcode=<n> 的失败 ack，断言账里 kind=failed 且 retcode=n",
    "  --reconnect                中间断开 1s 再重连，把同一 messageId 投第三次，断言仍不出站且 dup 再 +1",
    "  --ledger <path>            账文件，默认 <state>/ledger.jsonl",
    "  --once                     只跑第一条断言（不跑去重、重连、旧账重投、在飞窗口重投）",
    "                             断言 E 自动跑：拿账里早已存在的 key 重投，验去重认盘不认内存",
    "                             断言 F 自动跑（仅 send）：第一批还没发完就重投，验认领是同步占位",
    "  --no-inflight              关掉断言 F（不想在通道里制造在飞并发时）",
    "  --channel-log <path>       通道的 stdout 文件；给了就真数 plan 行（F4），不给按 echo 请求号反推",
    "  --batch                    只跑 G 组（合批）：drill 自己起假核 + 独用端口的通道，不碰 --url 上那个",
    "  --say                      只跑 G14 组（POST /say 主动开口），自己起通道，不调核",
    "  --decisions <path>         G 组读的决策账，默认 <state>/decisions.jsonl",
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
  // M9：群消息里「没定向的」默认不回（闲聊只按 1/10 抽样）。A–F 验的是去重/分泡/ack，
  // 不该被抽样噪声影响，所以给事件加一个「@ 她」的段——定向 = 一定回。
  if (opts.scene === "group" && Array.isArray(event.message)) {
    const selfId = Number(event.self_id);
    if (Number.isInteger(selfId)) {
      event.message = [{ type: "at", data: { qq: String(selfId) } }, ...event.message];
    }
  }
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
// 配额文件跟着 --state 走：演练自己的目录，绝不读生产账本。
const quotaStamps = (state: string, groupId: number): number[] => {
  const path = join(state, "quota.json");
  if (!existsSync(path)) return [];
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8")) as { groups?: Record<string, unknown> };
    const state = parsed;
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
// G19：假核按这些标记往回包里塞「闸判摘要」的五种形状（没有标记就一个字节都不加）。
const GATE_HIT = "GATEHIT";
const GATE_MISS = "GATEMISS";
const GATE_NONE = "GATENONE";
const GATE_BAD_STR = "GATEBADSTR";
const GATE_BAD_ARR = "GATEBADARR";
const GATE_BAD_NULL = "GATEBADNULL";
// M7：这条消息让假核回一个很低的 recalled 分（低于 recalledMinScore）——用来验 (c) 门。
const LOW_RECALL_MARK = "LOWRECALL";

const gateHitSummary = {
  at: "2026-11-06T05:00:00.000Z",
  audit: { status: "ok", hit: true, lcs_len: 42, threshold: 0.8 },
  rewrite: { status: "ok" },
};
const gateMissSummary = {
  at: "2026-11-06T05:01:00.000Z",
  audit: { status: "ok", hit: false, lcs_len: 0, threshold: 0.8 },
  rewrite: { status: "not-tried" },
};

// undefined = 回包里没有 gate 这一格；其余值原样塞进 gate.summary（含垃圾形状，用来验不搬运）。
const gateSummaryFor = (text: string): unknown => {
  if (text.includes(GATE_HIT)) return gateHitSummary;
  if (text.includes(GATE_MISS)) return gateMissSummary;
  if (text.includes(GATE_BAD_STR)) return "hit";
  if (text.includes(GATE_BAD_ARR)) return [1, 2, 3];
  if (text.includes(GATE_BAD_NULL)) return null;
  return undefined;
};

// 深等专用：键序稳定化，避免「同一个对象、不同键序」被误判成不等。
const stableJson = (value: unknown): string => {
  if (Array.isArray(value)) return "[" + value.map(stableJson).join(",") + "]";
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return (
      "{" +
      Object.keys(record)
        .sort()
        .map((key) => JSON.stringify(key) + ":" + stableJson(record[key]))
        .join(",") +
      "}"
    );
  }
  return JSON.stringify(value) ?? "undefined";
};
const SPEAKER_A = 3100000001;
const SPEAKER_B = 3100000002;
const SPEAKER_C = 3100000003;
// 第一段间隔必须 < openMs.quietOpen=2000ms：第一条消息自己就在 2s 到期，
// 所以「2.0s 间隔」压在的不是续发窗而是开批窗——实测 3 句 2.0s 间隔会劈成 [2] 与 [1] 两段。
const G1_OPEN_GAP_MS = 1600;
// 第二段间隔 2.6s：> 旧 sameSpeaker(2000ms)、< 新 continueMs.group(3000ms)，
// 这一段就是为了证明续发窗真的从 2s 变成了 3s。
const G1_CONTINUE_GAP_MS = 2600;
// G3 的间隔不能正好压在 continueMs=3000 上：3.0s 时第 4 条与 deadline 同刻到达（实测 3 段 [1,3,2]，
// 边界由「定时器先跑还是消息先到」决定），3.5s 离边界 500ms，实测稳定 4 段 [1,2,2,1]。
const G3_GAP_MS = 3500;

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

// 决策行现在带 machine.target：按目标归属比按时间窗可靠得多。
// 时间窗只能筛掉「更早」的行，筛不掉**相邻用例还在飞的那一段**——那会让计数提前满足。
const decisionRowsForTarget = (path: string, target: string, sinceWall: number): DecisionRow[] =>
  decisionRowsSince(path, sinceWall).filter((row) => row.machine?.target === target);

const waitForTargetDecisionRows = async (
  path: string,
  target: string,
  sinceWall: number,
  want: number,
  ms: number,
): Promise<DecisionRow[]> => {
  const deadline = Date.now() + ms;
  let rows = decisionRowsForTarget(path, target, sinceWall);
  while (rows.length < want && Date.now() < deadline) {
    await sleep(50);
    rows = decisionRowsForTarget(path, target, sinceWall);
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
      const base = text.includes(LONG_MARK)
        ? { ok: true, chunks: [{ text: "长".repeat(600), gap_before: 0, send_at: 0 }] }
        : text.includes(SILENT_MARK)
          ? { ok: true, chunks: [], send: false, reason: "刚在忙" }
          : text.includes(EMPTY_MARK)
            ? { ok: true, chunks: [] }
            : text.includes(TWO_MARK)
              ? { ok: true, chunks: [{ text: "第一泡", gap_before: 0, send_at: 0 }, { text: "第二泡", gap_before: 0, send_at: 0 }] }
              : { ok: true, chunks: [{ text: "在的", gap_before: 0, send_at: 0 }] };
      // 闸判摘要只在打了标记时挂上去：没标记的回包连 gate 这个键都不该有（那才是「没进闸」）。
      const summary = gateSummaryFor(text);
      // M7：默认让核「记得」这条（recalled 0.9 ≥ 阈值），老用例才会被 (c) 放行；
      // 带 LOW_RECALL_MARK 的回 0.2（低于阈值）——G20 的无定向闲聊靠它落在 no_mention。
      const recalled = { ok: true, recalled: [{ id: "mem-1", score: text.includes(LOW_RECALL_MARK) ? 0.2 : 0.9 }] };
      const withGate = summary === undefined ? base : { ...base, gate: { summary } };
      const payload = { ...withGate, ...recalled };
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

// 端口是不是空的：自己去 bind 一下（能 bind 就说明没人监听）。
const portFree = (port: number): Promise<boolean> =>
  new Promise<boolean>((resolveProbe) => {
    const probe = createNetServer();
    probe.once("error", () => resolveProbe(false));
    probe.listen(port, "127.0.0.1", () => probe.close(() => resolveProbe(true)));
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

// 起一个独用端口的通道：不抢生产端口，也不受线上通道影响（状态目录一律用 --state 传下去）。
const startChannel = async (
  coreUrl: string,
  mode: "send" | "silent",
  stateDir: string,
  gatePath?: string,
): Promise<Channel> => {
  let lastDump = "";
  for (let attempt = 0; attempt < 6; attempt += 1) {
    // 人拍的口径：演练一律 --port 6299。所以**先试 DRILL_PORT**；
    // 只有它被占（或者这一轮起失败了）才回退到空闲端口，并打一行说明——绝不悄悄换端口。
    let port = DRILL_PORT;
    if (attempt > 0) {
      port = await freePort();
    } else if (!(await portFree(DRILL_PORT))) {
      port = await freePort();
      say(DRILL_PORT + " 被占，演练改用 " + port);
    }
    const child: ChildProcess = spawn(
      process.execPath,
      [
        SERVER_PATH,
        "--core",
        coreUrl,
        "--port",
        String(port),
        "--state",
        stateDir,
        // G1–G19 不测 burst 阀：给一份 burstMs=0 的临时词表（系数仍从文件读，路径注入）。
        ...(gatePath === undefined ? [] : ["--gate", gatePath]),
      ],
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

// 私聊事件的造法：结构与 fixtures/event.private.text.json 一致（message_type=private、没有 group_id）。
// 私聊也要能连发三条进一段，所以这里不能只靠 fixture 那一条固定消息。
const buildPrivateEvent = (
  fixture: Record<string, unknown>,
  opts: { userId: number; messageId: number; text: string },
): Record<string, unknown> => {
  const event = buildEvent(fixture, {
    scene: "private",
    groupId: 0,
    userId: opts.userId,
    messageId: opts.messageId,
    time: Math.floor(Date.now() / 1000),
  }) as Record<string, any>;
  delete event.group_id;
  event.message = [{ type: "text", data: { text: opts.text } }];
  event.raw_message = opts.text;
  event.sender = { user_id: opts.userId, nickname: "私" + (opts.userId % 1000) };
  return event;
};

const runBatchSuite = async (config: Config): Promise<void> => {
  const fixture = JSON.parse(readFileSync(resolve(FIXTURES, "event.group.text.json"), "utf8"));
  const selfId = Number((fixture as Record<string, any>).self_id ?? 0);
  const policy = loadPolicy();
  const perGroupMax = policy.quota.perGroupMax;
  const defaultReplies = policy.outbound.defaultReplies;

  const core = await startFakeCore();
  // G1–G19 测的是合批 / 配额 / 上限 / 闸判摘要，不是 M7 的 burst 阀：
  // 给它们一份 burstMs=0 的临时词表（系数仍从文件读，只注入路径）。G20 用真的 data/gate.json。
  const realGate = loadGateWords();
  const fastGatePath = resolve(tmpdir(), `onebot-gate-fast-${process.pid}.json`);
  // 老用例（G1–G19）测的不是这道抽样阀：临时词表里把抽样改成人人都中（mod 1），burst 也关掉。
  writeFileSync(
    fastGatePath,
    `${JSON.stringify({ ...realGate, burstMs: 0, chitchatSampleMod: 1 })}\n`,
    "utf8",
  );
  let channel = await startChannel(core.url, "send", config.state, fastGatePath);
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
  // 私聊也用「每次新造一个号」：配额与判重都按会话走，同一个号跑第二遍会被配额挡住。
  let userSeq = 0;
  const userBase = 340_000_000 + (Date.now() % 100_000) * 10;
  const nextUser = (): number => {
    userSeq += 1;
    return userBase + userSeq;
  };
  const postPrivate = (userId: number, text: string): number => {
    const messageId = nextMessage();
    socket.send(JSON.stringify(buildPrivateEvent(fixture, { userId, messageId, text })));
    return messageId;
  };
  const hitsOf = (groupId: number): CoreHit[] => core.hits.filter((hit) => hit.body?.where?.groupId === groupId);
  const hitsOfPrivate = (userId: number): CoreHit[] => core.hits.filter((hit) => hit.body?.where?.userId === userId);
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
    // ---- G1：同一人连说三句（1.6s + 2.6s 间隔）→ 一段、一次核调用 ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      post(groupId, SPEAKER_A, "同人第 1 句");
      await sleep(G1_OPEN_GAP_MS);
      post(groupId, SPEAKER_A, "同人第 2 句");
      await sleep(G1_CONTINUE_GAP_MS);
      post(groupId, SPEAKER_A, "同人第 3 句");
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 14000);
      await sleep(500);
      const hits = hitsOf(groupId);
      const speakers = speakersOf(hits[0]?.body);
      add(
        "G1a",
        "同人 3 句（间隔 " + G1_OPEN_GAP_MS + "ms / " + G1_CONTINUE_GAP_MS + "ms）：只调核一次、决策账记 3 条",
        hits.length === 1 && rows[0]?.machine?.batch?.msgs === 3,
        "调核 " + hits.length + " 次，决策账 batch.msgs=" + String(rows[0]?.machine?.batch?.msgs),
      );
      add(
        "G1b",
        "段里 3 行、同一个 speaker",
        linesOf(hits[0]?.body).length === 3 && speakers.length === 3 && new Set(speakers).size === 1,
        "行数 " + linesOf(hits[0]?.body).length + " speakers=[" + speakers.join(",") + "]",
      );
      const tier = String(rows[0]?.machine?.batch?.tier ?? "");
      add(
        "G1c",
        "续发不分人：tier=continuing（旧账里的 sameSpeaker 只是历史）",
        tier === "continuing",
        "tier=" + tier,
      );
      const times = (Array.isArray(hits[0]?.body?.batch?.msgs) ? hits[0].body.batch.msgs : []).map((item: any) =>
        Date.parse(String(item?.at)),
      );
      const gap23 = times.length === 3 ? times[2] - times[1] : -1;
      add(
        "G1d",
        "第 3 句与前一条隔 " + G1_CONTINUE_GAP_MS + "ms 仍进同一段（续发窗 3000，不是旧的 sameSpeaker 2000）",
        gap23 >= G1_CONTINUE_GAP_MS - 50 && gap23 <= G1_CONTINUE_GAP_MS + 400,
        "实测第 2→3 句间隔 " + gap23 + "ms",
      );
    }

    // ---- G2：三个人交替 6 条（0.3s）→ 一段 6 行、speaker 交替 ----
    // 三个人是为了钉住「合批的单位是这一段在聊什么，不是谁说了几句」：
    // 6 条换来换去仍然是同一段（maxMsgs=6 在这一条上刚好也到期）。
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      const plan = [SPEAKER_A, SPEAKER_B, SPEAKER_C, SPEAKER_A, SPEAKER_B, SPEAKER_C];
      for (let i = 0; i < plan.length; i += 1) {
        post(groupId, plan[i], "交替第 " + (i + 1) + " 条");
        if (i < plan.length - 1) await sleep(300);
      }
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 10000);
      await sleep(500);
      const hits = hitsOf(groupId);
      const speakers = speakersOf(hits[0]?.body);
      const alternating = speakers.length === 6 && speakers.join(",") === plan.join(",");
      add(
        "G2a",
        "三个人交替 6 条（间隔 0.3s、3 秒内）：只调核一次",
        hits.length === 1,
        "调核 " + hits.length + " 次",
      );
      add(
        "G2b",
        "段里 6 行、speaker 按 A,B,C,A,B,C 交替（三个人都在同一段）",
        linesOf(hits[0]?.body).length === 6 && alternating,
        "行数 " + linesOf(hits[0]?.body).length + " speakers=[" + speakers.join(",") + "]",
      );
      const tier = String(rows[0]?.machine?.batch?.tier ?? "");
      add(
        "G2c",
        "换人不改 tier：6 条（含换人）仍然一路 continuing",
        tier === "continuing",
        "tier=" + tier,
      );
    }

    // ---- G3：A/B 交替 6 条（3s）→ 4 段，每段一次核调用 ----
    {
      // 合批已按会话分开：G3 用的是新群号，「上一条到达」为空 → 第一条必然是安静档（quietOpen 2000ms）。
      // 以前为了躲开「进程级 lastArrivalAt」要空等 31s，现在不需要了。
      const caseStart = Date.now();
      const groupId = nextGroup();
      const plan = [SPEAKER_A, SPEAKER_B, SPEAKER_A, SPEAKER_B, SPEAKER_A, SPEAKER_B];
      for (let i = 0; i < plan.length; i += 1) {
        post(groupId, plan[i], "G3-" + (i + 1));
        if (i < plan.length - 1) await sleep(G3_GAP_MS);
      }
      const hits = await waitForHits(groupId, 4, 26000);
      await sleep(700);
      const all = hitsOf(groupId);
      const counts = all.map((hit) => (Array.isArray(hit.body?.batch?.msgs) ? hit.body.batch.msgs.length : -1));
      const firsts = all.map((hit) => Date.parse(String(hit.body?.batch?.at ?? "")));
      const offsets = firsts.map((value) => Math.round(value - firsts[0]));
      const wantOffsets = [0, 3500, 10500, 17500];
      const speakerPlan = all.map((hit) => speakersOf(hit.body).join("+")).join(" | ");
      const wantSpeakers = [String(SPEAKER_A), SPEAKER_B + "+" + SPEAKER_A, SPEAKER_B + "+" + SPEAKER_A, String(SPEAKER_B)].join(" | ");
      add(
        "G3a",
        "间隔 " + G3_GAP_MS + "ms（> continueMs=3000）的 6 条 → 4 段（每段恰好一次核调用）",
        all.length === 4,
        "段数 " + all.length + "（核调用 " + hits.length + " 次）",
      );
      add("G3b", "每段条数 = [1,2,2,1]", counts.join(",") === "1,2,2,1", "[" + counts.join(",") + "]");
      add(
        "G3c",
        "每段 firstAt 相对首段 = [0,3500,10500,17500]（±300ms）",
        offsets.length === 4 && wantOffsets.every((want, index) => Number.isFinite(offsets[index]) && Math.abs(offsets[index] - want) <= 300),
        "[" + offsets.join(",") + "]ms（按相对差比，不依赖机器时钟精度）",
      );
      add("G3d", "每段 speaker 序列 = A | B,A | B,A | B（段间不串人）", speakerPlan === wantSpeakers, speakerPlan);

      // 墙钟：batch.at / msgs[].at 现在都是真时间，能和决策行的 ts 直接对表（不再是 1970 年）。
      const stamps = decisionRowsForTarget(config.decisions, "group:" + groupId, caseStart).map((row) => ({
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

    // ---- G17：群里换人不断开（两个人、1s 交替 4 条）→ 1 段 4 行 ----
    // 旧口径下「换人」自己就是一档（switchSpeaker=500ms），换一次人就提前到点；新口径续发不分人。
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      const plan = [SPEAKER_A, SPEAKER_B, SPEAKER_A, SPEAKER_B];
      for (let i = 0; i < plan.length; i += 1) {
        post(groupId, plan[i], "换人第 " + (i + 1) + " 条");
        if (i < plan.length - 1) await sleep(1000);
      }
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 12000);
      await sleep(600);
      const hits = hitsOf(groupId);
      const speakers = speakersOf(hits[0]?.body);
      const tier = String(rows[0]?.machine?.batch?.tier ?? "");
      add(
        "G17a",
        "两个不同人、1s 交替共 4 条 → 仍然 1 段（换人不再把一段话劈开）",
        hits.length === 1 && rows[0]?.machine?.batch?.msgs === 4,
        "调核 " + hits.length + " 次，决策账 msgs=" + String(rows[0]?.machine?.batch?.msgs),
      );
      add(
        "G17b",
        "段里 4 行、speaker 两人按 A,B,A,B 交替、tier=continuing",
        linesOf(hits[0]?.body).length === 4 && speakers.join(",") === plan.join(",") && tier === "continuing",
        "行数 " + linesOf(hits[0]?.body).length + " speakers=[" + speakers.join(",") + "] tier=" + tier,
      );
    }

    // ---- G18：六条上限（群里连发 8 条、间隔 0.3s）→ 两段 [6,2] ----
    // 最难写对的一条：凑满 6 条若只把 deadline 设成 now 而不先结束当前批，
    // 第 7 条会走「已到期 → 只并入」被塞进同一批，于是一段装下了 8 条。
    {
      const groupId = nextGroup();
      for (let i = 0; i < 8; i += 1) {
        post(groupId, i % 2 === 0 ? SPEAKER_A : SPEAKER_B, "上限第 " + (i + 1) + " 条");
        if (i < 7) await sleep(300);
      }
      await waitForHits(groupId, 2, 20000);
      await sleep(800);
      const all = hitsOf(groupId);
      const counts = all.map((hit) => (Array.isArray(hit.body?.batch?.msgs) ? hit.body.batch.msgs.length : -1));
      add(
        "G18a",
        "第 6 条一到就出手：恰好 2 段，条数 [6,2]",
        all.length === 2 && counts.join(",") === "6,2",
        "段数 " + all.length + "，条数 [" + counts.join(",") + "]",
      );
      const want = [SPEAKER_A, SPEAKER_B, SPEAKER_A, SPEAKER_B, SPEAKER_A, SPEAKER_B, SPEAKER_A, SPEAKER_B];
      const order = all.flatMap((hit) => speakersOf(hit.body));
      add(
        "G18b",
        "两段合计 8 条、顺序不乱（段间不重不漏）",
        order.length === 8 && order.join(",") === want.join(","),
        "合计 " + order.length + " 条：[" + order.join(",") + "]",
      );
    }

    // ---- G16：私聊三连发（3s 间隔）→ 1 段 3 行、只调核 1 次、只回 1 次；单条私聊 → 8s 内出手 ----
    // 私聊不再「每条即批 0 延迟」：现在也攒一段（3500ms 窗、8 秒顶、6 条上限）。
    {
      const userId = nextUser();
      const caseStart = Date.now();
      const start = mark();
      for (let i = 0; i < 3; i += 1) {
        postPrivate(userId, "私聊第 " + (i + 1) + " 句");
        if (i < 2) await sleep(3000);
      }
      const rows = await waitForTargetDecisionRows(config.decisions, "user:" + userId, caseStart, 1, 16000);
      await sleep(700);
      const hits = hitsOfPrivate(userId);
      const actions = actionsSince(start);
      const speakers = speakersOf(hits[0]?.body);
      const tier = String(rows[0]?.machine?.batch?.tier ?? "");
      add(
        "G16a",
        "私聊 3 条（间隔 3s）：只调核 1 次、决策账记 3 条",
        hits.length === 1 && rows[0]?.machine?.batch?.msgs === 3,
        "调核 " + hits.length + " 次，决策账 batch.msgs=" + String(rows[0]?.machine?.batch?.msgs),
      );
      add(
        "G16b",
        "段里 3 行、同一个 speaker、tier=continuing",
        linesOf(hits[0]?.body).length === 3 && speakers.length === 3 && new Set(speakers).size === 1 && tier === "continuing",
        "行数 " + linesOf(hits[0]?.body).length + " speakers=[" + speakers.join(",") + "] tier=" + tier,
      );
      add(
        "G16c",
        "只回 1 次（假核回 1 泡 → 恰好 1 个 action 帧）",
        actions.length === 1,
        "收到 " + actions.length + " 个 action 帧",
      );

      // 单条私聊：不再 0 延迟，按 privateOpen=3500ms 那一窗到期才出手；硬顶 8s 是它的上界。
      const solo = nextUser();
      const soloSent = Date.now();
      const soloStart = mark();
      postPrivate(solo, "私聊单条");
      const soloDeadline = Date.now() + 9000;
      while (actionsSince(soloStart).length === 0 && Date.now() < soloDeadline) await sleep(20);
      const soloFrames = framesSince(soloStart);
      const soloIndex = soloFrames.findIndex(isActionFrame);
      const soloLatency = soloIndex >= 0 ? frameTimes[soloStart + soloIndex] - soloSent : -1;
      const soloRows = await waitForTargetDecisionRows(config.decisions, "user:" + solo, soloSent, 1, 9000);
      await sleep(400);
      add(
        "G16d",
        "单条私聊：从到达到出手 ≤8000ms，且 ≥3500ms（证明真的按 privateOpen 攒了一段，不是 0 延迟）",
        soloLatency >= 0 && soloLatency <= 8000 && soloLatency >= 3500,
        "实测 " + soloLatency + "ms；决策账 tier=" + String(soloRows[soloRows.length - 1]?.machine?.batch?.tier ?? "") +
          " msgs=" + String(soloRows[soloRows.length - 1]?.machine?.batch?.msgs),
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
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, sent, 1, 5000);
      add(
        "G4",
        "@ 到出手 ≤1000ms（+ 送核/下发余量 200ms，等待上限本身是 mentionCapMs=1000）",
        latency >= 0 && latency <= 1200,
        "实测 " + latency + "ms；决策账 human=" + JSON.stringify(String(rows[rows.length - 1]?.human ?? "")),
      );
    }

    // ---- G5：5 条 ×1.6s → 第一段在 8s 硬顶出手，第 6 条落在第二段 ----
    // maxMsgs=6 之后一段最多 6 条，所以「8s 硬顶」只能用 5 条来证：
    // 没有硬顶的话第 5 条会把 deadline 推到 firstAt+9400，实测在 firstAt+8000 就出手了。
    {
      const groupId = nextGroup();
      const started = Date.now();
      for (let i = 0; i < 5; i += 1) {
        post(groupId, SPEAKER_A, "顶第 " + (i + 1) + " 条");
        if (i < 4) await sleep(1600);
      }
      // 等第一段到点（firstAt+8000）之后再补第 6 条：这条必须落在第二段。
      await sleep(2200);
      post(groupId, SPEAKER_A, "顶第 6 条");
      await waitForHits(groupId, 2, 24000);
      await sleep(700);
      const all = hitsOf(groupId);
      const first = all[0]?.body?.batch;
      const second = all[1]?.body?.batch;
      // 出手时刻直接对 firstAt 算，不带客户端网络那一截。
      const fire = first === undefined ? -1 : all[0].t - Date.parse(String(first.at ?? ""));
      const firstCount = Array.isArray(first?.msgs) ? first.msgs.length : -1;
      add(
        "G5a",
        "第一段在 8s 硬顶出手：出手−firstAt 落在 [7900,8600]、waited_ms ≤8000、条数 5",
        first !== undefined && fire >= 7900 && fire <= 8600 && Number(first.waited_ms) <= 8000 && firstCount === 5,
        "出手 firstAt+" + fire + "ms waited=" + String(first?.waited_ms) + "ms 条数=" + firstCount +
          "（客户端首条发出→出手 " + (all[0] === undefined ? -1 : all[0].t - started) + "ms）",
      );
      const span =
        Array.isArray(first?.msgs) && first.msgs.length > 0
          ? Date.parse(String(first.msgs[first.msgs.length - 1].at)) - Date.parse(String(first.msgs[0].at))
          : -1;
      add("G5b", "第一段跨度 ≤8000ms（硬顶没被后面的消息延长）", span >= 0 && span <= 8000, "跨度 " + span + "ms");
      const between = first !== undefined && second !== undefined ? Date.parse(String(second.at)) - Date.parse(String(first.at)) : -1;
      add(
        "G5c",
        "第 6 条落在第二段（第二段 firstAt − 第一段 firstAt ≥ 8000ms）",
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
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 5000);
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
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 8000);
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
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 8000);
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

    // ---- G19：决策账带「核的闸判摘要」（原样抄；缺了是 null，绝不拼空壳） ----
    {
      // ① 命中：audit.hit=true + lcs_len=42
      const hitGroup = nextGroup();
      const hitStart = Date.now();
      post(hitGroup, SPEAKER_A, "这轮命中闸 " + GATE_HIT);
      const hitRows = await waitForTargetDecisionRows(config.decisions, "group:" + hitGroup, hitStart, 1, 12000);
      await sleep(300);
      const hitRow = hitRows[hitRows.length - 1];
      add(
        "G19a",
        "命中（audit.hit=true）：machine.gate 与回包 summary 深等，含 lcs_len=42",
        stableJson(hitRow?.machine?.gate) === stableJson(gateHitSummary) &&
          (hitRow?.machine?.gate as any)?.audit?.lcs_len === 42,
        "gate=" + JSON.stringify(hitRow?.machine?.gate),
      );

      // ② 未命中：hit=false、rewrite.status=not-tried
      const missGroup = nextGroup();
      const missStart = Date.now();
      post(missGroup, SPEAKER_A, "这轮没命中闸 " + GATE_MISS);
      const missRows = await waitForTargetDecisionRows(config.decisions, "group:" + missGroup, missStart, 1, 12000);
      await sleep(300);
      const missRow = missRows[missRows.length - 1];
      add(
        "G19b",
        "未命中（hit=false、rewrite.status=not-tried）：同样深等带过去",
        stableJson(missRow?.machine?.gate) === stableJson(gateMissSummary) &&
          (missRow?.machine?.gate as any)?.rewrite?.status === "not-tried",
        "gate=" + JSON.stringify(missRow?.machine?.gate),
      );

      // ③ 回包里根本没有 gate 这一格 → null（不是 {}、不是 undefined）
      const noneGroup = nextGroup();
      const noneStart = Date.now();
      post(noneGroup, SPEAKER_A, "没进闸的那一轮 " + GATE_NONE);
      const noneRows = await waitForTargetDecisionRows(config.decisions, "group:" + noneGroup, noneStart, 1, 12000);
      await sleep(300);
      const noneRow = noneRows[noneRows.length - 1];
      add(
        "G19c",
        "回包里没有 gate → machine.gate === null（不是 {}、不是 undefined）",
        noneRows.length === 1 && noneRow?.machine?.gate === null,
        "gate=" + JSON.stringify(noneRow?.machine?.gate) + " typeof=" + typeof noneRow?.machine?.gate,
      );

      // ④ 垃圾形状：字符串 / 数组 / null → 一律 null（不搬运垃圾）。每种各用一个新的群，互不并批。
      const badGates: Array<Record<string, unknown> | null | undefined> = [];
      for (const marker of [GATE_BAD_STR, GATE_BAD_ARR, GATE_BAD_NULL]) {
        const groupId = nextGroup();
        const caseStart = Date.now();
        post(groupId, SPEAKER_A, "垃圾形状 " + marker);
        const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 12000);
        await sleep(200);
        badGates.push(rows[rows.length - 1]?.machine?.gate);
      }
      add(
        "G19d",
        "垃圾形状（summary 是字符串 / 数组 / null）一律 null",
        badGates.length === 3 && badGates.every((gate) => gate === null),
        "3 段 gate=[" + badGates.map((gate) => JSON.stringify(gate)).join(",") + "]",
      );

      // ⑤ /say：这条路不调核 → gate 恒 null
      const sayStart = Date.now();
      const sayGroup = nextGroup();
      const sayRes = await postJson("http://127.0.0.1:" + channel.port + "/say", {
        scene: "group",
        target: sayGroup,
        chunks: [{ text: "主动开口没有闸判" }],
      });
      const sayRows = await waitForTargetDecisionRows(config.decisions, "group:" + sayGroup, sayStart, 1, 12000);
      const sayRow = sayRows[sayRows.length - 1];
      add(
        "G19e",
        "/say 那条：machine.gate === null（不调核就没有闸判）",
        sayRes.json?.ok === true && sayRow?.machine?.gate === null,
        "ok=" + String(sayRes.json?.ok) + " gate=" + JSON.stringify(sayRow?.machine?.gate),
      );
    }

    // ---- G15：两个群交替说话 → 各自成段、各自回复（会话隔离） ----
    {
      const groupA = nextGroup();
      const groupB = nextGroup();
      const caseStart = Date.now();
      const start = mark();
      post(groupA, SPEAKER_A, "A 群第 1 句");
      await sleep(150);
      post(groupB, SPEAKER_B, "B 群第 1 句");
      await sleep(150);
      post(groupA, SPEAKER_A, "A 群第 2 句");
      await sleep(150);
      post(groupB, SPEAKER_B, "B 群第 2 句");
      // 两个目标各自按 target 取，再拼起来（时间窗只当上界，不再当归属依据）。
      const bothTargets = (): DecisionRow[] => [
        ...decisionRowsForTarget(config.decisions, "group:" + groupA, caseStart),
        ...decisionRowsForTarget(config.decisions, "group:" + groupB, caseStart),
      ];
      let decisions = bothTargets();
      {
        const deadline = Date.now() + 14000;
        while (decisions.length < 2 && Date.now() < deadline) {
          await sleep(50);
          decisions = bothTargets();
        }
      }
      await sleep(500);
      const hitsA = hitsOf(groupA);
      const hitsB = hitsOf(groupB);
      const actions = actionsSince(start);
      const targets = decisions.map((row) => String(row.machine?.target ?? "")).sort();
      const wantTargets = ["group:" + groupA, "group:" + groupB].sort();
      add(
        "G15a",
        "两群交替说话 → 恰好 2 段，决策行 target 各自正确、每段 2 条",
        decisions.length === 2 &&
          targets.join(",") === wantTargets.join(",") &&
          decisions.every((row) => row.machine?.batch?.msgs === 2),
        "决策 " + decisions.length + " 行 targets=[" + targets.join(",") + "] msgs=[" +
          decisions.map((row) => String(row.machine?.batch?.msgs)).join(",") + "]",
      );
      const linesOfBody = (body: Record<string, any> | undefined): number =>
        typeof body?.text === "string" ? body.text.split("\n").length : -1;
      const speakersOk =
        hitsA.length === 1 &&
        hitsB.length === 1 &&
        (hitsA[0]?.body?.batch?.msgs ?? []).every((item: any) => item.id === SPEAKER_A) &&
        (hitsB[0]?.body?.batch?.msgs ?? []).every((item: any) => item.id === SPEAKER_B);
      add(
        "G15b",
        "每群各调核 1 次，段里只带本群的行（不串群、不串人）",
        speakersOk && linesOfBody(hitsA[0]?.body) === 2 && linesOfBody(hitsB[0]?.body) === 2,
        "A 群调核 " + hitsA.length + " 次（" + linesOfBody(hitsA[0]?.body) + " 行）、B 群 " + hitsB.length +
          " 次（" + linesOfBody(hitsB[0]?.body) + " 行）",
      );
      const actionGroups = actions.map((frame) => Number(frame?.params?.group_id)).sort();
      add(
        "G15c",
        "两段各回一条，action 分别进各自的群（不串群）",
        actions.length === 2 && actionGroups.join(",") === [groupA, groupB].sort().join(","),
        "action " + actions.length + " 个 → groups=[" + actionGroups.join(",") + "]",
      );
    }

    // ---- G13：出站全败（协议端不回 ack）→ 配额退还 ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      ackEnabled = false; // 不回 ack：第一泡会 ack 超时，第二泡记 unsent，这一轮 sent=0
      const messageId = post(groupId, SPEAKER_A, "这条发不出去 " + TWO_MARK);
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 30000);
      await sleep(600);
      const mine = rowsFor(readRowsFile(config.ledger), "mid:" + messageId);
      const outboundFail = mine.filter((row) => row.kind === "uncertain" || row.kind === "failed" || row.kind === "unsent");
      const decision = rows[rows.length - 1];
      const stamps = quotaStamps(config.state, groupId);
      add(
        "G13a",
        "出站全败（sent=0）也不写配额（M9：inbound 全程不 charge）",
        stamps.length === 0,
        "配额 " + stamps.length + " 条",
      );
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
      await waitForTargetDecisionRows(config.decisions, "group:" + controlGroup, controlStart, 1, 12000);
      await sleep(400);
      const controlActions = actionsSince(controlMark).length;
      add(
        "G13d",
        "对照组：正常 ack 的那段照常发出一条，且同样不写配额",
        controlActions === 1 && quotaStamps(config.state, controlGroup).length === 0,
        "action " + controlActions + " 个，配额 " + quotaStamps(config.state, controlGroup).length + " 条",
      );
    }

    // ---- G7：silent → 一条 action 都不出，决策账 silent_mode + 账里 blocked ----
    {
      await channel.stop();
      channel = await startChannel(core.url, "silent", config.state, fastGatePath);
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
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 8000);
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
      const rows = await waitForTargetDecisionRows(config.decisions, "group:" + groupId, caseStart, 1, 8000);
      await sleep(1200);
      const hits = hitsOf(groupId);
      const actionCount = actionsSince(start).length;
      const blockedRows = rowsFor(readRowsFile(config.ledger), "mid:" + messageId).filter((row) => row.kind === "blocked");
      const stamps = quotaStamps(config.state, groupId);
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
    // ---- G21：M9 两档判据（定向一定回 / 闲聊每 10 段抽 1 段），用真实 data/gate.json ----
    {
      await channel.stop();
      channel = await startChannel(core.url, "send", config.state);
      ackEnabled = true;
      try {
        socket.close();
      } catch {
        /* 收尾而已 */
      }
      socket = await connect(channel.url);
      say(
        "G21：默认词表通道 " + channel.url + "（chitchatSampleMod=" + realGate.chitchatSampleMod +
          "，burstMs=" + realGate.burstMs + "）",
      );

      const groupId = nextGroup();
      const tgt = "group:" + groupId;
      const caseStart = Date.now();
      const start = mark();
      let sendWall: number | null = null;
      const rowsSoFar = (): DecisionRow[] => decisionRowsForTarget(config.decisions, tgt, caseStart);
      const postAndWait = async (text: string, mention = false): Promise<DecisionRow | undefined> => {
        const want = rowsSoFar().length + 1;
        post(groupId, SPEAKER_A, text, mention);
        const rows = await waitForTargetDecisionRows(config.decisions, tgt, caseStart, want, 25000);
        const row = rows[rows.length - 1];
        if (row?.machine?.decided === "sent") sendWall = Date.now();
        return row;
      };
      const waitOutsideBurst = async (): Promise<void> => {
        while (sendWall !== null && Date.now() - sendWall < realGate.burstMs + 1200) await sleep(100);
      };

      const plan = new Map<number, "at" | "nickname" | "chitchat">();
      for (const at of [1, 15, 29]) plan.set(at, "at");
      for (const nick of [5, 25]) plan.set(nick, "nickname");
      for (let index = 1; index <= 30; index += 1) if (!plan.has(index)) plan.set(index, "chitchat");

      for (let ordinal = 1; ordinal <= 30; ordinal += 1) {
        const kind = plan.get(ordinal)!;
        if (kind === "chitchat" && ordinal % realGate.chitchatSampleMod === 0) await waitOutsideBurst();
        if (kind === "at") await postAndWait("叫她一声 " + ordinal, true);
        else if (kind === "nickname") await postAndWait("小肥鱼在吗 " + ordinal);
        else await postAndWait("闲聊第 " + ordinal + " 句");
      }
      await sleep(600);

      const rows = rowsSoFar();
      const directed = rows.filter((row) =>
        ["at", "nickname", "quote", "private"].includes(String(row.machine?.direct?.rule)),
      );
      const chitchatSent = rows.filter(
        (row) => row.machine?.direct?.rule === "chitchat" && row.machine?.decided === "sent",
      );
      const skipped = rows.filter((row) => row.machine?.reason_code === "chitchat_skipped");
      const coreCalls = hitsOf(groupId).length;
      add(
        "G21a",
        "定向 5 条（3 @ + 2 外号）全部发出",
        rows.length === 30 && directed.length === 5 && directed.every((row) => row.machine?.decided === "sent"),
        "30 段、定向 " + directed.length + " 条（" +
          directed.map((row) => String(row.machine?.direct?.rule)).join(",") + "）",
      );
      add(
        "G21b",
        "闲聊发出 2–3 条，且 ordinal 是 " + realGate.chitchatSampleMod + " 的倍数",
        chitchatSent.length >= 2 &&
          chitchatSent.length <= 3 &&
          chitchatSent.every((row) => Number(row.machine?.direct?.ordinal) % realGate.chitchatSampleMod === 0),
        "发出 " + chitchatSent.length + " 条，ordinal=[" +
          chitchatSent.map((row) => String(row.machine?.direct?.ordinal)).join(",") + "]",
      );
      add(
        "G21c",
        "其余闲聊各一行 chitchat_skipped（人话「没回：闲聊，没抽到」）",
        skipped.length === 25 - chitchatSent.length && skipped.every((row) => row.human === "没回：闲聊，没抽到"),
        "chitchat_skipped=" + skipped.length + " 行（应有 " + (25 - chitchatSent.length) + " 行）",
      );
      add(
        "G21d",
        "被抽掉的闲聊一次核都没调（核调用 = 5 定向 + 抽中的闲聊）",
        coreCalls === 5 + chitchatSent.length,
        "调核 " + coreCalls + " 次（应为 " + (5 + chitchatSent.length) + "）",
      );
      const stamps = quotaStamps(config.state, groupId);
      add("G21e", "inbound 不写配额（该群在 quota.json 里没有条目）", stamps.length === 0, "配额 " + stamps.length + " 条");

      // 定向不受 burst：先用 /say 真发一条（刷新「上一次真正发出」），紧接着投一条 @ 她。
      // 这一段的决策时刻落在 burst 窗口内——如果是闲聊就会被压掉，但定向照发。
      const burstStart = Date.now();
      const burstMark = mark();
      const warmRes = await postJson("http://127.0.0.1:" + channel.port + "/say", {
        scene: "group",
        target: groupId,
        chunks: [{ text: "先发一条垫着" }],
      });
      post(groupId, SPEAKER_A, "紧接着叫她", true);
      const burstRows = await waitForTargetDecisionRows(config.decisions, tgt, burstStart, 2, 30000);
      await sleep(400);
      const burstInbound = burstRows.filter((row) => row.machine?.source === "inbound").pop();
      const burstActions = actionsSince(burstMark).length;
      add(
        "G21f",
        "定向不受 burst：刚发过一条，紧随的 @ 她仍然发出（闲聊在这个窗口里会被压）",
        warmRes.json?.ok === true && burstInbound?.machine?.decided === "sent" && burstActions === 2,
        "预热 /say ok=" + String(warmRes.json?.ok) + "，@ 那条 reason=" + String(burstInbound?.machine?.reason_code) +
          "，action " + burstActions + " 个",
      );

      const decisionOf = async (message: Array<Record<string, unknown>>): Promise<DecisionRow | undefined> => {
        const group = nextGroup();
        const since = Date.now();
        const event = buildGroupEvent(fixture, {
          groupId: group,
          speaker: SPEAKER_A,
          messageId: nextMessage(),
          text: "",
          mention: false,
          selfId,
        });
        (event as Record<string, any>).message = message;
        socket.send(JSON.stringify(event));
        const list = await waitForTargetDecisionRows(config.decisions, "group:" + group, since, 1, 20000);
        return list[list.length - 1];
      };
      const commandRow = await decisionOf([{ type: "text", data: { text: "/help" } }]);
      const stickerRow = await decisionOf([{ type: "face", data: { id: "14" } }]);
      const imageRow = await decisionOf([{ type: "image", data: { file: "a.jpg" } }]);
      add("G21g", "命令（/ 开头）→ reason=command", commandRow?.machine?.reason_code === "command",
        "reason=" + String(commandRow?.machine?.reason_code));
      add("G21h", "纯表情 → reason=sticker", stickerRow?.machine?.reason_code === "sticker",
        "reason=" + String(stickerRow?.machine?.reason_code));
      add("G21i", "纯图无字 → reason=media_only", imageRow?.machine?.reason_code === "media_only",
        "reason=" + String(imageRow?.machine?.reason_code));
    }

  } finally {
    await channel.stop();
    await core.close();
  }

  const allPass = printTable();
  process.exit(allPass ? 0 : 1);
};

// /say 的 ledger key 是 `say-<n>`，而 n 是**进程内**序号：通道重启后会从 say-1 重来，
// 所以同一个 key 在账里会有好几组。演练按「本段开始时刻之后的行」归属，再核对 key 与响应 id 一致。
const sayRowsSince = (path: string, caseStart: number): LedgerRow[] =>
  readRowsFile(path).filter((row) => {
    if (typeof row.key !== "string" || !row.key.startsWith("say-")) return false;
    const stamp = typeof row.ts === "string" ? Date.parse(row.ts) : Number.NaN;
    return Number.isFinite(stamp) && stamp >= caseStart - 100;
  });

// ---------- G14 组：POST /say（主动开口） ----------
// 这条路一个核调用都没有，所以不需要假核：通道随便给个不可达的 --core 地址就行（/say 永远不碰它）。
const postJson = async (url: string, body: unknown): Promise<{ status: number; json: Record<string, any> | null }> => {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  let json: Record<string, any> | null = null;
  try {
    json = (await response.json()) as Record<string, any>;
  } catch {
    json = null;
  }
  return { status: response.status, json };
};

const runSaySuite = async (config: Config): Promise<void> => {
  const policy = loadPolicy();
  const defaultReplies = policy.outbound.defaultReplies;
  const perGroupMax = policy.quota.perGroupMax;
  let channel = await startChannel("http://127.0.0.1:1", "send", config.state);
  ackEnabled = true;
  socket = await connect(channel.url);
  const sayUrl = (): string => "http://127.0.0.1:" + channel.port + "/say";
  let groupSeq = 0;
  const groupBase = 760_000_000 + (Date.now() % 90_000) * 100;
  const nextGroup = (): number => {
    groupSeq += 1;
    return groupBase + groupSeq;
  };
  say("G14：/say 通道 " + channel.url + "（mode=send；核地址故意不可达——这条路不调核）");
  add("G14-0", "/say 的通道起来了（send 模式，不需要核）", true, channel.url);

  try {
    // ---- G14a：真发 ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      const start = mark();
      const res = await postJson(sayUrl(), {
        scene: "group",
        target: groupId,
        chunks: [
          { text: "主动第一泡", send_at: 0 },
          { text: "主动第二泡", send_at: 0 },
        ],
      });
      await sleep(300);
      const actions = actionsSince(start);
      const id = String(res.json?.id ?? "");
      const sentRows = sayRowsSince(config.ledger, caseStart).filter(
        (row) => row.kind === "sent" && row.key === id,
      );
      const decision = decisionRowsForTarget(config.decisions, "group:" + groupId, caseStart).pop();
      add(
        "G14a",
        "群 2 泡：2 个 action、2 行 sent retcode=0、决策 sent+proactive（msgs=0）、响应 ok:true sent:2",
        res.status === 200 &&
          res.json?.ok === true &&
          res.json?.sent === 2 &&
          actions.length === 2 &&
          sentRows.length === 2 &&
          sentRows.every((row) => row.retcode === 0) &&
          decision?.machine?.reason_code === "sent" &&
          decision?.machine?.source === "proactive" &&
          decision?.machine?.batch?.msgs === 0 &&
          decision?.machine?.target === "group:" + groupId,
        "status=" + res.status + " ok=" + String(res.json?.ok) + " sent=" + String(res.json?.sent) +
          " actions=" + actions.length + " sentRows=" + sentRows.length +
          " decision=" + String(decision?.machine?.reason_code) + "/" + String(decision?.machine?.source) +
          " msgs=" + String(decision?.machine?.batch?.msgs),
      );
      add(
        "G14a2",
        "echo 仍是 req-<n>-<i>、泡序号从 0 递增",
        checkEchoSequence(actions.map((frame) => String(frame?.echo))).length === 0,
        actions.map((frame) => String(frame?.echo)).join(","),
      );
    }

    // ---- G14c：群配额打满（先跑，后面的 G14d 要借它的「已满」状态） ----
    const quotaGroup = nextGroup();
    {
      const responses: Array<Record<string, any> | null> = [];
      for (let index = 0; index < perGroupMax; index += 1) {
        const res = await postJson(sayUrl(), { scene: "group", target: quotaGroup, chunks: [{ text: "配额" + (index + 1) }] });
        responses.push(res.json);
      }
      const start = mark();
      const seventh = await postJson(sayUrl(), { scene: "group", target: quotaGroup, chunks: [{ text: "该被挡了" }] });
      await sleep(400);
      const actions = actionsSince(start);
      const lastRow = decisionRowsForTarget(config.decisions, "group:" + quotaGroup, Date.now() - 120_000).pop();
      add(
        "G14c",
        "同群连发 " + perGroupMax + " 次后第 " + (perGroupMax + 1) + " 次：ok:false quota_exceeded、0 action、决策 quota_exceeded",
        responses.every((json) => json?.ok === true) &&
          seventh.status === 200 &&
          seventh.json?.ok === false &&
          seventh.json?.reason_code === "quota_exceeded" &&
          actions.length === 0 &&
          lastRow?.machine?.reason_code === "quota_exceeded" &&
          lastRow?.machine?.target === "group:" + quotaGroup,
        "前 " + perGroupMax + " 次 ok=" + JSON.stringify(responses.map((json) => json?.ok)) +
          " 第 " + (perGroupMax + 1) + " 次 ok=" + String(seventh.json?.ok) + " reason=" + String(seventh.json?.reason_code) +
          " actions=" + actions.length + " 决策末行=" + String(lastRow?.machine?.reason_code),
      );
    }

    // ---- G14d：群配额已满时，私聊照样能开口 ----
    {
      const start = mark();
      const res = await postJson(sayUrl(), { scene: "private", target: 3000000003, chunks: [{ text: "私聊也说一句" }] });
      await sleep(300);
      const actions = actionsSince(start);
      add(
        "G14d",
        "群配额已满时私聊 /say 仍 ok:true 且真发出去（send_private_msg）",
        res.json?.ok === true &&
          actions.length === 1 &&
          actions[0]?.action === "send_private_msg" &&
          actions[0]?.params?.user_id === 3000000003,
        "ok=" + String(res.json?.ok) + " action=" + String(actions[0]?.action) + " user=" + String(actions[0]?.params?.user_id),
      );
    }

    // ---- G14e：三道上限（4 泡 > maxChunks） ----
    {
      const groupId = nextGroup();
      const caseStart = Date.now();
      const start = mark();
      const res = await postJson(sayUrl(), {
        scene: "group",
        target: groupId,
        chunks: [1, 2, 3, 4].map((index) => ({ text: "第" + index + "泡", send_at: 0 })),
      });
      await sleep(300);
      const actions = actionsSince(start);
      const text = String(actions[0]?.params?.message?.[0]?.data?.text ?? "");
      const decision = decisionRowsForTarget(config.decisions, "group:" + groupId, caseStart).pop();
      add(
        "G14e",
        "4 泡（>maxChunks=" + policy.outbound.maxChunks + "）→ 只发 1 条默认短句、决策 replaced_too_long",
        res.json?.ok === true && actions.length === 1 && defaultReplies.includes(text) &&
          decision?.machine?.reason_code === "replaced_too_long",
        "ok=" + String(res.json?.ok) + " actions=" + actions.length + " text=" + JSON.stringify(text) +
          " reason=" + String(decision?.machine?.reason_code),
      );
    }

    // ---- G14f：坏输入一律 400 ----
    {
      const cases: Array<{ name: string; body: unknown }> = [
        { name: "scene=unknown", body: { scene: "unknown", target: 700000001, chunks: [{ text: "x" }] } },
        { name: "target 非数字", body: { scene: "group", target: "700000001", chunks: [{ text: "x" }] } },
        { name: "chunks:[]", body: { scene: "group", target: 700000001, chunks: [] } },
      ];
      const results: string[] = [];
      let allBad = true;
      for (const item of cases) {
        const res = await postJson(sayUrl(), item.body);
        if (res.status !== 400 || res.json?.ok !== false) allBad = false;
        results.push(item.name + ":" + res.status + "/ok=" + String(res.json?.ok));
      }
      add("G14f", "坏输入（scene / target / chunks 各一条）一律 400 + ok:false", allBad, results.join(" "));
    }

    // ---- G14g：别的路径一律 404 ----
    {
      const res = await postJson("http://127.0.0.1:" + channel.port + "/other", { hello: 1 });
      add(
        "G14g",
        "非 /say 路径 → 404 且 message 固定",
        res.status === 404 && res.json?.message === "只有 POST /say",
        "status=" + res.status + " message=" + JSON.stringify(String(res.json?.message ?? "")),
      );
    }

    // ---- G14b：silent（换一条 silent 通道） ----
    {
      await channel.stop();
      channel = await startChannel("http://127.0.0.1:1", "silent", config.state);
      ackEnabled = false;
      try {
        socket.close();
      } catch {
        /* 收尾而已 */
      }
      socket = await connect(channel.url);
      const groupId = nextGroup();
      const caseStart = Date.now();
      const start = mark();
      const res = await postJson(sayUrl(), {
        scene: "group",
        target: groupId,
        chunks: [
          { text: "silent 第一泡", send_at: 0 },
          { text: "silent 第二泡", send_at: 0 },
        ],
      });
      await sleep(Math.min(3000, config.wait));
      const actions = actionsSince(start);
      const id = String(res.json?.id ?? "");
      const blocked = sayRowsSince(config.ledger, caseStart).filter(
        (row) => row.kind === "blocked" && row.key === id,
      );
      const decision = decisionRowsForTarget(config.decisions, "group:" + groupId, caseStart).pop();
      add(
        "G14b",
        "silent：0 个 action、1 行 blocked（text 是整段原文）、决策 silent_mode+proactive、响应 ok:false",
        res.status === 200 &&
          res.json?.ok === false &&
          actions.length === 0 &&
          blocked.length === 1 &&
          String(blocked[0]?.text ?? "").includes("silent 第一泡") &&
          String(blocked[0]?.text ?? "").includes("silent 第二泡") &&
          decision?.machine?.reason_code === "silent_mode" &&
          decision?.machine?.source === "proactive",
        "ok=" + String(res.json?.ok) + " reason=" + String(res.json?.reason_code) + " actions=" + actions.length +
          " blocked=" + blocked.length + " text=" + JSON.stringify(String(blocked[0]?.text ?? "")) +
          " decision=" + String(decision?.machine?.reason_code) + "/" + String(decision?.machine?.source),
      );
    }
  } finally {
    await channel.stop();
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
  // G14 组（/say）同样自成一体：这条路不调核，所以不需要假核。
  if (config.say) {
    await runSaySuite(config);
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
