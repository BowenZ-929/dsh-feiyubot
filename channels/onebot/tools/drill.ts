// drill.ts —— 一条命令的断言式演练：它扮演协议端，按冻结契约给通道下断言。
// 前提：通道已在 --url 跑着、核也已在跑（drill 自己不起进程、不发 HTTP）。
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
  help: boolean;
};

const raw = (argv: string[], name: string): string | undefined => {
  const index = argv.indexOf("--" + name);
  if (index < 0) return undefined;
  const value = argv[index + 1];
  return value && !value.startsWith("--") ? value : undefined;
};

const has = (argv: string[], name: string): boolean => argv.includes("--" + name);

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
    groupId: Number(raw(argv, "group") ?? "700000001"),
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
    "  --group <id>               群号（群消息的目标），默认 700000001",
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
    say("收到非 JSON 帧：" + text.slice(0, 120));
    return;
  }
  inbox.push(frame);
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

const main = async (): Promise<void> => {
  const config = parseConfig(process.argv.slice(2));
  if (config.help) {
    printHelp();
    process.exit(0);
  }
  failRetcode = config.failRetcode;
  ackEnabled = config.expectMode === "send";

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
    add(
      "F3",
      "账里该 key 恰好多 1 行 dup，note 含「在飞」",
      dups.length === 1 && String(dups[0]?.note ?? "").includes("在飞"),
      "dup=" + dups.length + " note=" + JSON.stringify(String(dups[0]?.note ?? "")),
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
