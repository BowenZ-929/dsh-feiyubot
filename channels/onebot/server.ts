// OneBot v11 反向 WS 通道：我们开 127.0.0.1:6200，协议端（"会说 OneBot 的进程"）连进 /ws。
// M1（默认）：收到消息只记日志——原始事件 + parseEvent 解析结果，不发任何东西、不写账、不建 .state。
// M2（加 --core <核地址>）：消息转给核，把核回的 chunks 按 send_at 分时发回来源会话。
// M4：不再是「一条消息一次核调用」，而是「一段消息一次核调用」——
//   入站 → 判重 → 落一行 batched → BatchHub 攒段 → 到期取件 → 每段查配额 → 调核一次 →
//   三道上限 → 每泡出站 → 每段恰好一行决策账（.state/decisions.jsonl）。
// M5：同一条出站管线多一个内部入口——`POST /say`（没人说话、她自己想说时用它）。
//   门 → 配额 → 三道上限 → planChunks + runBatch → 每段一行决策账（`machine.source=proactive`）。
// 解析不在这里重写：一律走 M0 已验收的 parse.ts；合批 / 渲染 / 上限 / 配额 / 决策账都是现成模块，这里只接线。
import WebSocket, { WebSocketServer } from "ws";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseEvent, type ParsedEvent, type Where } from "./parse.ts";
import { LEDGER_RELATIVE_PATH, Ledger, dedupKey, dedupNote, readMode, sceneOf, targetOf, type Scene } from "./gate.ts";
import {
  ACK_TIMEOUT_MS,
  planChunks,
  readCoreTimeout,
  parseCoreResponse,
  runBatch,
  settleRound,
  type AckOutcome,
  type Chunk,
  type CoreAttempt,
} from "./outbox.ts";
import { loadPolicy, type Policy } from "./policy.ts";
import { BatchHub, type BatchSnapshot } from "./batch.ts";
import { renderSegment } from "./segment.ts";
import { enforceLimits } from "./compose.ts";
import { DECISIONS_RELATIVE_PATH, DecisionLog, type GateInput } from "./decide.ts";
import { QUOTA_RELATIVE_PATH, Quota } from "./quota.ts";

const HOST = "127.0.0.1";
const WS_PATH = "/ws";
const HERE = dirname(fileURLToPath(import.meta.url));
const LEDGER_PATH = join(HERE, LEDGER_RELATIVE_PATH);
// 决策账与配额状态跟账同一个目录：一本讲「发了什么」，一本讲「为什么没发」，一本讲「这个小时说了几次」。
const DECISIONS_PATH = join(HERE, DECISIONS_RELATIVE_PATH);
const QUOTA_PATH = join(HERE, QUOTA_RELATIVE_PATH);

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

// 端口默认 6200 一个字没变；--port 只给演练用（drill 要起自己的通道，不能抢线上那个 6200）。
const PORT_RAW = argOf("--port", "6200");
const PORT_NUMBER = Number(PORT_RAW);
const PORT = Number.isInteger(PORT_NUMBER) && PORT_NUMBER > 0 && PORT_NUMBER < 65536 ? PORT_NUMBER : 6200;
const PORT_WARN = PORT === PORT_NUMBER ? null : `--port 认不出 ${JSON.stringify(PORT_RAW)}，回落 6200`;

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
if (PORT_WARN !== null) log("warn", PORT_WARN);
log("gate", gate.mode === "send" ? "mode=send（会真发出去）" : "mode=silent（想发但被门挡住）");

// 账是全进程一份：去重集合在启动时就把历史读进来，重连和重启都不重发。
const ledger = new Ledger({ path: LEDGER_PATH, onWarn: (message) => log("warn", message) });

// M4 的几件东西只在给了 --core 时才有：M1 不许建 .state，也不许写任何账（读盘不是写盘，但没必要读）。
let policy: Policy | null = null;
let hub: BatchHub | null = null;
let decisions: DecisionLog | null = null;
let quota: Quota | null = null;
if (CORE_URL) {
  try {
    policy = loadPolicy();
  } catch (error) {
    // 系数读不出来就别上线：静默回落等于把每一档时长都变成一个隐藏常量。
    log("error", `系数读不出来，进程退出：${(error as Error).message}`);
    process.stdout.write("", () => process.exit(1));
  }
  if (policy !== null) {
    hub = new BatchHub(policy.batch);
    decisions = new DecisionLog({ path: DECISIONS_PATH, policy, onWarn: (message) => log("warn", message) });
    quota = new Quota({ path: QUOTA_PATH, policy, onWarn: (message) => log("warn", message) });
  }
}

let connectionSeq = 0;
let eventSeq = 0;
let requestSeq = 0;

// /say 没有「来源 socket」：主动开口只能往当前连着的那个协议端发。没有连接就是发不出去（不是崩溃）。
let lastSocket: WebSocket | null = null;

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

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

// 一段消息的「账的固定部分」：这一段的每一行都带同样的 key / 场景 / 目标。
// key 取这一段最后一句话的 key——单条成段时它就是这个群/私聊里唯一能对上的那个键。
type BatchMeta = { key: string; messageId: number | null; socket: WebSocket; who: ParsedEvent["who"]; where: Where; hasImage: boolean };
const batchMeta = new Map<string, BatchMeta>();

// 每个会话（group:<id> / user:<id>）一个定时器：永远指向 hub.state(key) 报的那个 deadline。
// 一个群热闹不该把另一个群的取件推迟或提前——定时器也必须按会话来排。
const batchTimers = new Map<string, ReturnType<typeof setTimeout>>();

const disarmTimer = (key: string): void => {
  const timer = batchTimers.get(key);
  if (timer !== undefined) {
    clearTimeout(timer);
    batchTimers.delete(key);
  }
};

const armTimer = (key: string): void => {
  disarmTimer(key);
  if (hub === null) return;
  const state = hub.state(key);
  // 私聊的排队段（上一条还没取走）不等时间：有排队就立刻取。
  if (!state.open && state.queued === 0) return;
  const remaining =
    state.queued > 0 || !state.open || state.deadline === null
      ? 0
      : Math.max(0, state.deadline - Date.now());
  batchTimers.set(key, setTimeout(() => onDeadline(key), remaining));
};

const onDeadline = (key: string): void => {
  batchTimers.delete(key);
  if (hub === null) return;
  const snapshot = hub.consume(key, Date.now());
  if (snapshot === null) {
    // 到点但取不到件（deadline 被后到的消息推后了/已经取走）：只把这一个会话的定时器对回新 deadline。
    armTimer(key);
    return;
  }
  const meta = batchMeta.get(snapshot.batchId);
  batchMeta.delete(snapshot.batchId);
  if (meta === undefined) {
    // 没有 meta 就没有目标与 key：宁可这一步什么都不发，也不猜一个对象发出去。
    log("error", `段 ${snapshot.batchId} 找不到来源（没有 meta），本轮丢弃`);
    armTimer(key);
    return;
  }
  void runSegment(snapshot, meta);
  armTimer(key);
};

// 每段一次核调用。这一段里所有「为什么没发」的结论都收成恰好一行决策账。
const runSegment = async (snapshot: BatchSnapshot, meta: BatchMeta): Promise<void> => {
  if (policy === null || decisions === null || quota === null) return;
  const id = ++requestSeq;
  const started = Date.now();
  const elapsed = (): string => `${Date.now() - started}ms`;
  const where: Where = meta.where;
  // scene 只认三个值：拿不到 id 就是 unknown，那种消息后面一步都不会出站。
  const scene: Scene = sceneOf(where);
  const target = targetOf(where);
  const batch = { key: meta.key, messageId: meta.messageId, scene, target };
  const logOf = (tag: string, message: string): void => log(tag, message);
  const isSocketOpen = (): boolean => meta.socket.readyState === WebSocket.OPEN;
  const send = (frame: string): void => meta.socket.send(frame);
  const ledgerSink = { appendNew: (row: Parameters<Ledger["appendNew"]>[0]) => ledger.appendNew(row) };

  const gates: GateInput[] = [];
  let decided = false;
  // 只有真扣过配额的那一段才谈得上退回：silent 不扣，所以也不退。
  let charged = false;
  // 一批只写一行：先到的那句实话就是这一段的结论，后面谁再想写都不许覆盖。
  const decide = (extra: GateInput[], reason: string | null = null): void => {
    if (decided) return;
    decided = true;
    const all = [...gates, ...extra];
    try {
      decisions.append({
        scene: snapshot.scene,
        mentioned: snapshot.mentioned,
        target,
        // 入站路径永远是 inbound；主动开口（/say）显式传 proactive。
        source: "inbound",
        // 核给的理由是自由文本，由 machine.reason 单独承载；why 只描述门，不许再塞门名里。
        reason,
        // msgs 是条数，不是 segment.ts 里那份消息数组：名字撞了，写错会让面板把条数算成 NaN。
        batch: {
          id: snapshot.batchId,
          msgs: snapshot.msgs.length,
          waited_ms: snapshot.waited_ms,
          tier: snapshot.tier,
          at: new Date(snapshot.firstAt).toISOString(),
        },
        gates: all,
      });
    } catch (error) {
      log("error", `段 ${snapshot.batchId} 决策账没写成：${(error as Error).message}`);
    }
  };
  // 确认「她没出手」之后把这一次配额退回去：扣了却没回，等于白占这个群一次额度。
  const refund = (why: string): void => {
    if (!charged) return;
    charged = false;
    quota.refund(target, Date.now());
    log("quota", `段 ${snapshot.batchId} target=${target} 没出手（${why}），退回这一次配额`);
  };

  try {
    log(
      `req#${id}`,
      `段 ${snapshot.batchId} msgs=${snapshot.msgs.length} tier=${snapshot.tier} waited=${snapshot.waited_ms}ms @=${snapshot.mentioned} → ${target}`,
    );

    // ① 门在最外层，mode 永远是第一道。silent 时**照样调核**——门是这套里唯一的变量，
    //    silent 的账要能回答「如果门开着她会说什么」，所以 blocked 行必须有 text。
    const isSend = gate.mode === "send";
    gates.push({ name: "mode", code: "silent_mode", hit: !isSend });

    // ② 群配额：只对 send 扣（silent 本来就不会出手，扣了纯属白占这个群的额度）。
    if (isSend) {
      const charge = quota.charge(target, Date.now(), snapshot.mentioned);
      if (!charge.ok) {
        gates.push({ name: "quota", code: "quota_exceeded", hit: true });
        log("quota", `段 ${snapshot.batchId} target=${target} 配额用尽（used=${charge.used}），本轮不调核、不发`);
        decide([]);
        return;
      }
      charged = true;
      gates.push({ name: "quota", code: "quota_exceeded", hit: false });
      // 被 @ 时可以突破配额：这是「有人叫她」而不是「她抢话」，门认得出来就照记一笔。
      if (charge.overridden) {
        gates.push({ name: "mention", code: "sent", hit: false });
        log("quota", `段 ${snapshot.batchId} target=${target} 配额已满（used=${charge.used}），被 @ 突破，照回`);
      }
    }

    // ③ 送核：text 用 renderSegment 的逐行渲染，batch 带上这一段的元信息；其余字段照旧。
    const rendered = renderSegment(snapshot);
    log(`req#${id}`, `② 交给核 POST ${CORE_URL}${CORE_PATH}（${snapshot.msgs.length} 行，${elapsed()}）`);

    let attempt: CoreAttempt;
    try {
      const response = await fetch(`${CORE_URL}${CORE_PATH}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          who: meta.who,
          where: meta.where,
          scene,
          text: rendered.text,
          hasImage: meta.hasImage,
          messageId: meta.messageId,
          batch: rendered.batch,
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

    // 收尾一律走既有 settleRound：error / empty / silent / blocked 的文案与前缀一个字都不新写，
    // 发出去那一段也仍然是它调 runBatch（分泡 + send_at + ack + 落账）。
    const settle = async (value: CoreAttempt): Promise<Awaited<ReturnType<typeof settleRound>>> =>
      settleRound({
        mode: gate.mode,
        batch,
        where,
        scene,
        attempt: value,
        origin: Date.now(),
        requestSeq: id,
        elapsed,
        ledger: ledgerSink,
        isGateOpen: () => gate.mode === "send",
        isSocketOpen,
        send,
        waitAck,
        sleep,
        now: () => Date.now(),
        log: logOf,
      });

    if (attempt.kind !== "payload") {
      await settle(attempt);
      refund("核出错");
      decide([{ name: "core", code: "core_error", hit: true }]);
      return;
    }

    const reply = parseCoreResponse(attempt.payload);
    if (!reply.ok) {
      // 认不出就交给 settleRound 记 error（前缀「响应认不出：」），这一轮不发。
      await settle({ kind: "unreadable", reason: reply.reason });
      refund("响应认不出");
      decide([{ name: "core", code: "core_error", hit: true }]);
      return;
    }

    // 核主动说「这轮不说话」（send:false）：不是故障。账上落一行 silent（理由进 note），
    // 决策账仍然是 core_empty，核给的理由进 machine.reason。
    if (reply.silent !== undefined) {
      await settle(attempt);
      refund("她这轮不想说");
      decide([{ name: "core", code: "core_empty", hit: true }], reply.silent.reason);
      return;
    }

    // ④ 三道上限：整批越界就整批换成一句短的，绝不截半句。
    const limited = enforceLimits(reply.chunks, snapshot.batchId, policy);
    if (limited.reason === "core_empty") {
      // 用空 chunks 走 settleRound：账上照样恰好一行 empty（「她这轮没说」）。
      await settle({ kind: "payload", payload: { ok: true, chunks: [] } });
      refund("她这轮没说");
      decide([{ name: "limits", code: "core_empty", hit: true }]);
      return;
    }

    const result = await settle({ kind: "payload", payload: { ok: true, chunks: limited.chunks } });
    if (result.kind === "empty") {
      // 核回了泡，但全是空白：和「一个泡都没有」是同一件事，账上那行 empty 已经写了。
      refund("全是空白泡");
      decide([{ name: "limits", code: "core_empty", hit: true }]);
      return;
    }
    // 出站全败（failed / uncertain / unsent / ack 超时 / 发送前连接已断）：她一条都没发出去，
    // 这一次配额不该占着。sent>=1 算出手过，不退——部分发出也是一句真话。
    // 退款的四个入口（core_error / core_empty / send:false / 出站全败）共用同一个 refund()，
    // 它带 charged 标志，所以一条路径只会真退一次，绝不重复退。
    // 只有门开着（send）才谈得上「出站全败」：silent 下 sent=0 是门挡的，由 mode 那道门说明。
    if (result.batch !== null && result.batch.sent === 0 && gate.mode === "send") {
      refund(`sent=0，stopped=${result.batch.stopped ?? "—"}`);
      // 出站全败有自己的 code（outbound_failed）：ack 超时 / 连接断开时我们不知道对方收没收到，
      // 措辞是「没确认」——不是「核坏了」，也不是「没发」。decided 仍是 silent，退款照旧只退一次。
      gates.push({ name: "outbound", code: "outbound_failed", hit: true });
    }
    if (limited.reason === "replaced_too_long") {
      // 她真发了一条（换成一句短的），这一次不算白占配额。
      decide([{ name: "limits", code: "replaced_too_long", hit: true }]);
      return;
    }
    decide([{ name: "limits", code: "core_empty", hit: false }]);
  } catch (error) {
    // 认领早就放了，这一段也早就落过 batched 行；异常只补一行 error，不重试、不重发。
    const message = (error as Error).message;
    log("error", `段 ${snapshot.batchId} 处理异常，本轮收尾：${message}`);
    ledger.appendNew({
      kind: "error",
      mode: gate.mode,
      key: meta.key,
      messageId: meta.messageId,
      scene,
      target,
      note: `处理异常：${message}`,
    });
    gates.push({ name: "internal", code: "core_error", hit: true });
    decide([]);
  }
};

// 入站：判重 → 攒段 → 落一行 batched → 重排定时器。这一段里一次核调用都还没有发生。
const ingest = (event: ParsedEvent, socket: WebSocket): void => {
  if (hub === null) return;
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

  // 认领是同步的内存占位：判重和入段之间不许有 await，否则同一条会被收进两次。
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
    const mentioned = event.mentionedSelf || event.mentionedAll;
    hub.observe(
      {
        senderId: event.who.id,
        // 群名片优先：段里每一行都要有个能读的人名。
        senderName: event.who.card ?? event.who.nickname,
        text: event.text,
        mentioned,
        bareImage: event.isBareImage,
        sticker: event.isSticker,
        where: event.where,
      },
      Date.now(),
    );

    const state = hub.state(target);
    const batchId = state.batchId ?? "unknown";
    const before = batchMeta.get(batchId);
    batchMeta.set(batchId, {
      key,
      messageId: event.messageId,
      socket,
      who: event.who,
      where: event.where,
      hasImage: event.hasImage || (before?.hasImage ?? false),
    });
    // 每条入站恰好一行 batched：消息账从此讲的是「这条消息进没进来」，不是「这条消息发没发」。
    ledger.appendNew({
      kind: "batched",
      mode: gate.mode,
      key,
      messageId: event.messageId,
      scene,
      target,
      note: `batch=${batchId} tier=${String(state.tier)} @=${mentioned ? "是" : "否"}`,
    });
    log(
      "batch",
      `key=${key} → ${batchId} tier=${String(state.tier)} count=${state.count} ` +
        `deadline=${state.deadline === null ? "—" : `${Math.max(0, Math.round(state.deadline - Date.now()))}ms 后`}`,
    );
  } finally {
    // 认领只是入段期间的互斥：batched 行已经让这个 key 在去重集合里落地了，这里必须放掉。
    ledger.release(key);
  }

  // 只重排这个会话自己的定时器（target 就是会话键：group:<id> / user:<id>）。
  armTimer(target);
};

// 心跳与生命周期事件照收（parse 照跑、序号照走），只是不刷日志：
// 心跳默认 1 秒一条，原样写会一分钟撑 60 行、把真消息淹掉。summary 每分钟一行。
const HEARTBEAT_LOG_EVERY_MS = 60_000;
let heartbeatSeen = 0;
let lastHeartbeatLogAt = 0;

const handleEvent = (socket: WebSocket, raw: string): void => {
  eventSeq += 1;
  const id = eventSeq;
  const isLifecycle = raw.includes('"post_type":"meta_event"');
  if (isLifecycle) {
    heartbeatSeen += 1;
    const now = Date.now();
    if (now - lastHeartbeatLogAt >= HEARTBEAT_LOG_EVERY_MS) {
      log("recv", `心跳/生命周期事件累计 ${heartbeatSeen} 条（每秒 1 条照收，日志每分钟一行）`);
      lastHeartbeatLogAt = now;
    }
  } else {
    log("recv", `#${id} 原始事件 ${raw}`);
  }

  const parsed = parseEvent(raw);
  if (!parsed.ok) {
    if (!isLifecycle) log("parse", `#${id} 解析失败：${parsed.reason}`);
    return;
  }
  log(
    "parse",
    `#${id} ok who=[${describeWho(parsed)}] where=[${describeWhere(parsed)}] ` +
      `text=${JSON.stringify(parsed.text)} hasImage=${parsed.hasImage}`,
  );
  if (!CORE_URL) return;
  try {
    ingest(parsed, socket);
  } catch (error) {
    // 入段本身不许因为一条畸形消息把通道带下去。
    log("error", `#${id} 入段失败：${(error as Error).message}`);
  }
};

// ---------- M5：内部入口 POST /say（主动开口） ----------
// 它走的是**同一条**出站管线：门 → 配额 → 三道上限 → planChunks + runBatch → 一行决策账。
// 不复制发送逻辑：分泡、send_at、ack、每泡一行账全在 outbox.ts 里，这里只接线。
const BODY_LIMIT = 64 * 1024;

type SayInput = { scene: "group" | "private"; target: number; chunks: Chunk[] };
type SayResult = {
  ok: boolean;
  id: string;
  source: "proactive";
  sent: number;
  stopped: string | null;
  reason_code: string;
};

let saySeq = 0;

const sendJson = (response: ServerResponse, status: number, body: unknown): void => {
  try {
    const text = JSON.stringify(body);
    response.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(text) });
    response.end(text);
  } catch (error) {
    // 对端早走了：说一声就够，不许把进程带下去。
    log("warn", `回 /say 失败：${(error as Error).message}`);
  }
};

// 请求体最多 64KB：主动开口是内部接口，不该有人往这里灌大包。
const readBody = (request: IncomingMessage): Promise<{ ok: true; text: string } | { ok: false; message: string }> =>
  new Promise((resolve) => {
    let size = 0;
    let tooBig = false;
    const parts: Buffer[] = [];
    request.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > BODY_LIMIT) {
        tooBig = true;
        parts.length = 0;
        return; // 继续排空但不再留数据：直接 destroy 会让 400 发不回去。
      }
      if (!tooBig) parts.push(chunk);
    });
    request.on("end", () => {
      if (tooBig) resolve({ ok: false, message: "请求体超过 64KB，不收" });
      else resolve({ ok: true, text: Buffer.concat(parts).toString("utf8") });
    });
    request.on("error", (error) => resolve({ ok: false, message: `读请求体失败：${error.message}` }));
  });

// 校验只认这三样：scene / target / chunks。请求体里的 source 一律忽略——这条路的来源恒为 proactive。
const parseSayBody = (raw: unknown): { ok: true; value: SayInput } | { ok: false; message: string } => {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ok: false, message: "请求体应为 JSON 对象" };
  }
  const body = raw as Record<string, unknown>;
  const scene = body.scene;
  // 主动开口没有「不知道发给谁」这一档：只认这两个值。
  if (scene !== "group" && scene !== "private") {
    return { ok: false, message: `scene 只认 group 或 private（拿到 ${JSON.stringify(scene)}）` };
  }
  const target = body.target;
  if (typeof target !== "number" || !Number.isFinite(target) || !Number.isInteger(target) || target <= 0) {
    return { ok: false, message: `target 必须是有限正整数（拿到 ${JSON.stringify(target)}）` };
  }
  const rawChunks = body.chunks;
  if (!Array.isArray(rawChunks) || rawChunks.length === 0) {
    return { ok: false, message: "chunks 必须是非空数组" };
  }
  const chunks: Chunk[] = [];
  for (let index = 0; index < rawChunks.length; index += 1) {
    const item: unknown = rawChunks[index];
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      return { ok: false, message: `chunks[${index}] 必须是对象` };
    }
    const text = (item as Record<string, unknown>).text;
    if (typeof text !== "string" || text.length === 0) {
      return { ok: false, message: `chunks[${index}].text 必须是非空字符串` };
    }
    const rawSendAt = (item as Record<string, unknown>).send_at;
    let sendAt = 0;
    if (rawSendAt !== undefined) {
      if (typeof rawSendAt !== "number" || !Number.isFinite(rawSendAt)) {
        return { ok: false, message: `chunks[${index}].send_at 必须是数字（秒）` };
      }
      sendAt = rawSendAt;
    }
    chunks.push({ text, gap_before: 0, send_at: sendAt });
  }
  return { ok: true, value: { scene, target, chunks } };
};

// 主动开口这一段：与入站共用 decide / quota / enforceLimits / planChunks / runBatch，
// 差别只在「没有核、没有入站消息、source=proactive」。发送逻辑一行都没复制。
const runSay = async (input: SayInput): Promise<SayResult> => {
  if (policy === null || decisions === null || quota === null) {
    throw new Error("M1 模式（没给 --core）没有决策账 / 配额，/say 不接活");
  }
  const id = `say-${(saySeq += 1)}`;
  const where: Where =
    input.scene === "group" ? { scene: "group", groupId: input.target } : { scene: "private", userId: input.target };
  const scene: Scene = input.scene;
  const target = targetOf(where);
  // 主动开口没有来源 socket：往当前连着的那个协议端发；一个都没连就按「发不出去」收尾。
  const socket = lastSocket;
  const gates: GateInput[] = [];
  let charged = false;
  let decidedCode: string | null = null;

  const decide = (extra: GateInput[]): string => {
    if (decidedCode !== null) return decidedCode;
    try {
      const row = decisions.append({
        scene,
        mentioned: false,
        target,
        source: "proactive",
        reason: null,
        // msgs=0：这一段没有入站消息（别塞 chunks 数——那是「泡」，不是「消息」）。
        batch: { id, msgs: 0, waited_ms: 0, tier: "proactive", at: new Date().toISOString() },
        gates: [...gates, ...extra],
      });
      decidedCode = row.machine.reason_code;
    } catch (error) {
      log("error", `${id} 决策账没写成：${(error as Error).message}`);
      decidedCode = "core_error";
    }
    return decidedCode;
  };
  const refund = (why: string): void => {
    if (!charged) return;
    charged = false;
    quota.refund(target, Date.now());
    log("quota", `${id} target=${target} 没出手（${why}），退回这一次配额`);
  };

  const isSend = gate.mode === "send";
  gates.push({ name: "mode", code: "silent_mode", hit: !isSend });

  if (isSend) {
    // 主动开口也计群配额（不然它能刷屏）；mentioned 恒 false：主动开口没有「被 @」。
    const charge = quota.charge(target, Date.now(), false);
    if (!charge.ok) {
      gates.push({ name: "quota", code: "quota_exceeded", hit: true });
      log("quota", `${id} target=${target} 配额用尽（used=${charge.used}），主动开口被挡`);
      return { ok: false, id, source: "proactive", sent: 0, stopped: null, reason_code: decide([]) };
    }
    charged = true;
    gates.push({ name: "quota", code: "quota_exceeded", hit: false });
  }

  // 三道上限：越界就整批换成一句短的（sha256 按 id 选，同一批永远同一句）。
  const limited = enforceLimits(input.chunks, id, policy);
  const plan = planChunks(limited.chunks, Date.now());
  log("say", `${id} → ${target} chunks=${plan.length} send_at=[${plan.map((entry) => entry.send_at).join(",")}] mode=${gate.mode}`);

  const result = await runBatch({
    mode: gate.mode,
    batch: { key: id, messageId: null, scene, target },
    where,
    plan,
    requestSeq: (requestSeq += 1),
    ledger: { appendNew: (row) => ledger.appendNew(row) },
    isGateOpen: () => gate.mode === "send",
    isSocketOpen: () => socket !== null && socket.readyState === WebSocket.OPEN,
    send: (frame) => {
      if (socket === null) throw new Error("没有协议端连着");
      socket.send(frame);
    },
    waitAck,
    sleep,
    now: () => Date.now(),
    log,
  });

  // 同入站：silent 下 sent=0 是门挡的（blocked 行已经说了），不算「出站全败」。
  if (result.sent === 0 && isSend) {
    // 与入站同一套：出站全败（含「没有连接」「全是空白泡」）→ 退配额。
    if (plan.length === 0) {
      refund("chunks 全是空白泡");
      gates.push({ name: "limits", code: "core_empty", hit: true });
    } else {
      refund(`sent=0，stopped=${result.stopped ?? "—"}`);
      gates.push({ name: "outbound", code: "outbound_failed", hit: true });
    }
  }
  if (limited.reason === "replaced_too_long") {
    gates.push({ name: "limits", code: "replaced_too_long", hit: true });
  } else if (result.sent > 0) {
    gates.push({ name: "limits", code: "core_empty", hit: false });
  }

  const reasonCode = decide([]);
  return {
    ok: reasonCode === "sent" || reasonCode === "replaced_too_long",
    id,
    source: "proactive",
    sent: result.sent,
    stopped: result.stopped,
    reason_code: reasonCode,
  };
};

const handleHttp = async (request: IncomingMessage, response: ServerResponse): Promise<void> => {
  const path = (request.url ?? "").split("?")[0];
  if (path !== "/say" || request.method !== "POST") {
    sendJson(response, 404, { ok: false, message: "只有 POST /say" });
    return;
  }
  const body = await readBody(request);
  if (!body.ok) {
    sendJson(response, 400, { ok: false, message: body.message });
    return;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(body.text);
  } catch (error) {
    sendJson(response, 400, { ok: false, message: `请求体不是合法 JSON：${(error as Error).message}` });
    return;
  }
  const say = parseSayBody(parsed);
  if (!say.ok) {
    sendJson(response, 400, { ok: false, message: say.message });
    return;
  }
  if (policy === null || decisions === null || quota === null) {
    // M1（没给 --core）：没有系数、没有账、也不许建 .state——这条路直说不接活。
    sendJson(response, 503, { ok: false, message: "M1 模式（没给 --core）：/say 不接活" });
    return;
  }
  try {
    const result = await runSay(say.value);
    log(
      "say",
      `${result.id} 完成：sent=${result.sent} stopped=${result.stopped ?? "—"} reason=${result.reason_code} ok=${result.ok}`,
    );
    sendJson(response, 200, result);
  } catch (error) {
    log("error", `/say 处理异常：${(error as Error).message}`);
    sendJson(response, 500, { ok: false, message: `服务内部错误：${(error as Error).message}` });
  }
};

// HTTP 与 WS 共用同一个端口：WS 挂在 /ws 上，其余交给 handleHttp（只认 POST /say，别的一律 404）。
const httpServer = createServer((request, response) => {
  void handleHttp(request, response);
});

const wss = new WebSocketServer({ server: httpServer, path: WS_PATH });

// 监听是否已经成功：监听阶段出错（典型 EADDRINUSE）与运行期出错是两回事，不能混成一句话。
let listening = false;

httpServer.on("listening", () => {
  listening = true;
  log("listen", `反向 WS 已监听 ws://${HOST}:${PORT}${WS_PATH}，等客户端连进来`);
  log("listen", `内部入口 POST http://${HOST}:${PORT}/say 已就绪（主动开口，走同一条出站管线）`);
  log(
    "mode",
    CORE_URL
      ? `M4：核地址 ${CORE_URL}${CORE_PATH}，超时=${CORE_TIMEOUT.ms}ms，按「一段消息一次核调用」回话`
      : "M1：只记日志，不发任何东西",
  );
});

wss.on("connection", (socket, request) => {
  const id = ++connectionSeq;
  const peer = `${request.socket.remoteAddress}:${request.socket.remotePort}`;
  log("conn", `#${id} 已连接 来自 ${peer}，当前在线 ${wss.clients.size}`);
  // /say 没有来源 socket：记住最近连上的这个，主动开口就发给它。
  lastSocket = socket;

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
    if (lastSocket === socket) lastSocket = null;
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

// ws 挂在已有的 http.Server 上时，会把 server 的 error 原样转发成 wss 的 'error'。
// 没有监听器就是「Unhandled 'error' event」——进程会带着堆栈崩，把我们那句人话吃掉。
// 所以这里留一个空的：真正的处置在下面 httpServer 的监听器里（只打一行、退出码 1）。
wss.on("error", () => {
  /* 已由 httpServer 的 error 监听器接管 */
});

// 监听失败（典型 EADDRINUSE）必须还是「打一行 + 退出码 1」：WS 挂在 http.Server 上之后，
// 这个错误由 http.Server 发出，所以处置挂在它身上；上面的 wss 监听器只负责别崩。
httpServer.on("error", (error) => {
  if (listening) {
    log("error", `服务出错：${error.message}`);
    return;
  }
  log("error", `监听失败：${error.message}，进程退出`);
  process.stdout.write("", () => process.exit(1));
});

httpServer.listen(PORT, HOST);

process.on("SIGINT", () => {
  log("exit", "收到 Ctrl-C，关闭监听");
  // 有定时器的会话 + 还开着批的会话，两边都停一次（它们不一定重合）。
  for (const key of new Set<string>([...batchTimers.keys(), ...(hub?.openKeys() ?? [])])) disarmTimer(key);
  wss.close(() => {
    httpServer.close(() => process.exit(0));
  });
});
