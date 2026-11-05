import { test } from "node:test";
import assert from "node:assert/strict";
import { ACK_TIMEOUT_MS, CORE_TIMEOUT_MS, buildAction, echoFor, parseCoreResponse, planChunks, readCoreTimeout, runBatch, settleRound } from "./outbox.ts";
import type { AckOutcome, BatchInput, CoreAttempt, PlanEntry, SettleInput } from "./outbox.ts";
import { LEDGER_RELATIVE_PATH, Ledger, dedupNote, makeRow, type LedgerRow, type Mode, type Scene } from "./gate.ts";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

type Harness = {
  input: BatchInput;
  events: string[];
  logs: string[];
  rows: LedgerRow[];
};

const threeChunks = (): PlanEntry[] =>
  planChunks(
    [
      { text: "第一泡", gap_before: 0, send_at: 0 },
      { text: "第二泡", gap_before: 1.6, send_at: 1.6 },
      { text: "第三泡", gap_before: 1.6, send_at: 3.2 },
    ],
    0,
  );

// 一次批次的全套假件：不发真网络、不写真账、不睡真时间。
const harness = (
  options: {
    mode?: Mode;
    plan?: PlanEntry[];
    acks?: AckOutcome[];
    gateOpen?: () => boolean;
    socketOpen?: () => boolean;
  } = {},
): Harness => {
  const events: string[] = [];
  const logs: string[] = [];
  const rows: LedgerRow[] = [];
  const acks: AckOutcome[] = [...(options.acks ?? [])];
  let clock = 0;

  const input: BatchInput = {
    mode: options.mode ?? "send",
    batch: { key: "mid:2000000001", messageId: 2000000001, scene: "group", target: "group:700000001" },
    where: { scene: "group", groupId: 700000001 },
    plan: options.plan ?? threeChunks(),
    requestSeq: 1,
    ledger: {
      // 和真账一样补齐字段默认值：否则用例断言的是假件的空洞，不是真账的形状。
      appendNew: (row) => {
        rows.push(makeRow(row, "2026-01-01T00:00:00.000Z"));
        return row;
      },
    },
    isGateOpen: options.gateOpen ?? (() => true),
    isSocketOpen: options.socketOpen ?? (() => true),
    send: (frame) => {
      events.push(`send:${frame}`);
    },
    waitAck: async (echo) => {
      events.push(`ack:${echo}`);
      return acks.shift() ?? { kind: "ack", status: "ok", retcode: 0, note: "" };
    },
    sleep: async (ms) => {
      events.push(`sleep:${ms}`);
      clock += ms;
    },
    now: () => clock,
    log: (tag, message) => {
      logs.push(`${tag} ${message}`);
    },
  };

  return { input, events, logs, rows };
};

const echoes = (events: string[]): string[] =>
  events.filter((event) => event.startsWith("send:")).map((event) => JSON.parse(event.slice(5)).echo);

test("核的响应：chunks 优先，text 兜底，其余认不出", () => {
  const chunks = parseCoreResponse({
    ok: true,
    chunks: [{ text: "甲", gap_before: 0, send_at: 0 }, { text: "乙", gap_before: 1, send_at: 1.5 }],
  });
  assert.equal(chunks.ok, true);
  if (chunks.ok) {
    assert.equal(chunks.chunks.length, 2);
    assert.deepEqual(chunks.chunks[1], { text: "乙", gap_before: 1, send_at: 1.5 });
  }

  const single = parseCoreResponse({ ok: true, text: "一句" });
  assert.equal(single.ok, true);
  if (single.ok) assert.deepEqual(single.chunks, [{ text: "一句", gap_before: 0, send_at: 0 }]);

  const both = parseCoreResponse({ ok: true, text: "被忽略", chunks: [{ text: "甲", send_at: 0 }] });
  assert.equal(both.ok, true);
  if (both.ok) assert.deepEqual(both.chunks.map((chunk) => chunk.text), ["甲"]);

  for (const payload of [null, "字符串", { ok: false, error: "闸住了" }, { ok: true }, { ok: true, chunks: [{ text: 1 }] }]) {
    const reply = parseCoreResponse(payload);
    assert.equal(reply.ok, false, `应当认不出：${JSON.stringify(payload)}`);
    if (!reply.ok) assert.ok(reply.reason.length > 0);
  }
});

test("分泡计划：send_at 换算成绝对目标，空泡不占位", () => {
  const plan = planChunks(
    [
      { text: "先", gap_before: 0, send_at: 0 },
      { text: "   ", gap_before: 0, send_at: 0.5 },
      { text: "后", gap_before: 1, send_at: 1.6 },
    ],
    1000,
  );
  assert.deepEqual(plan.map((entry) => [entry.index, entry.text, entry.sendAt]), [
    [0, "先", 1000],
    [1, "后", 2600],
  ]);
  assert.deepEqual(planChunks([], 500), []);
});

test("echo 形如 req-1-0，action 按场景选群/私聊", () => {
  assert.equal(echoFor(1, 0), "req-1-0");
  assert.equal(echoFor(12, 3), "req-12-3");
  assert.deepEqual(buildAction({ scene: "group", groupId: 700000001 }, "早", "req-1-0"), {
    action: "send_group_msg",
    params: { group_id: 700000001, message: [{ type: "text", data: { text: "早" } }] },
    echo: "req-1-0",
  });
  assert.deepEqual(buildAction({ scene: "private", userId: 3000000003 }, "早", "req-1-1"), {
    action: "send_private_msg",
    params: { user_id: 3000000003, message: [{ type: "text", data: { text: "早" } }] },
    echo: "req-1-1",
  });
});

test("silent：一个 send 都不调用，只落一条 blocked（带整句与泡数）", async () => {
  const run = harness({ mode: "silent" });
  const result = await runBatch(run.input);

  assert.deepEqual(echoes(run.events), []);
  assert.deepEqual(run.events, [], "连 sleep 都不该发生：门根本没开");
  assert.equal(result.sent, 0);
  assert.equal(run.rows.length, 1);
  assert.equal(run.rows[0].kind, "blocked");
  assert.equal(run.rows[0].mode, "silent");
  assert.equal(run.rows[0].text, "第一泡第二泡第三泡");
  assert.equal(run.rows[0].send_at, null);
  assert.equal(run.rows[0].index, null);
  assert.equal(run.rows[0].note, "chunks=3 send_at=[0,1.6,3.2]");
  assert.ok(run.logs.some((line) => line.startsWith("blocked ") && line.includes("mode=silent")));
});

test("send：按 send_at 等到点发，严格串行（上一泡 ack 之后才有下一泡）", async () => {
  const run = harness();
  const result = await runBatch(run.input);

  assert.equal(result.sent, 3);
  assert.deepEqual(echoes(run.events), ["req-1-0", "req-1-1", "req-1-2"]);
  // 起点 0 计划 0/1600/3200：首泡 send_at=0 立刻发，随后每泡只等自己的间隔到了就发。
  assert.deepEqual(run.events.filter((event) => event.startsWith("sleep:")), ["sleep:1600", "sleep:1600"]);

  const order = run.events.map((event) => (event.startsWith("send:") ? `send:${JSON.parse(event.slice(5)).echo}` : event));
  assert.deepEqual(order, [
    "send:req-1-0",
    "ack:req-1-0",
    "sleep:1600",
    "send:req-1-1",
    "ack:req-1-1",
    "sleep:1600",
    "send:req-1-2",
    "ack:req-1-2",
  ]);
  assert.deepEqual(run.rows.map((row) => [row.kind, row.index, row.retcode, row.status]), [
    ["sent", 0, 0, "ok"],
    ["sent", 1, 0, "ok"],
    ["sent", 2, 0, "ok"],
  ]);
  assert.equal(run.logs.filter((line) => line.startsWith("chunk ")).length, 3);
});

test("send_at 递减到 0：已过点的泡立刻发，不等也不补发", async () => {
  const plan = planChunks(
    [
      { text: "一", gap_before: 0, send_at: 3 },
      { text: "二", gap_before: 0, send_at: 1.6 },
      { text: "三", gap_before: 0, send_at: 0 },
    ],
    0,
  );
  const run = harness({ plan });
  await runBatch(run.input);

  assert.deepEqual(run.events.filter((event) => event.startsWith("sleep:")), ["sleep:3000"]);
  assert.equal(echoes(run.events).length, 3);
  assert.equal(run.events.filter((event) => event.startsWith("send:")).length, 3);
});

test("ack retcode!=0：该泡 failed，批次中止，后面全是 unsent，且不重试", async () => {
  const run = harness({
    acks: [{ kind: "ack", status: "failed", retcode: 100, note: `data.message="发送失败"` }],
  });
  const result = await runBatch(run.input);

  assert.equal(echoes(run.events).length, 1, "失败后不许再发下一泡");
  assert.equal(result.sent, 0);
  assert.deepEqual(run.rows.map((row) => [row.kind, row.index]), [
    ["failed", 0],
    ["unsent", 1],
    ["unsent", 2],
  ]);
  assert.equal(run.rows[0].retcode, 100);
  assert.equal(run.rows[0].status, "failed");
  assert.match(run.rows[0].note, /发送失败/);
  assert.ok(run.logs.some((line) => line.startsWith("fail ") && line.includes("retcode=100")));
  assert.ok(run.logs.some((line) => line.startsWith("unsent ")));
});

test("ack 超时：该泡 uncertain，批次中止，绝不自动重发", async () => {
  const run = harness({ acks: [{ kind: "timeout" }] });
  await runBatch(run.input);

  assert.equal(echoes(run.events).length, 1);
  assert.deepEqual(run.rows.map((row) => row.kind), ["uncertain", "unsent", "unsent"]);
  assert.match(run.rows[0].note, /ack 超时（echo=req-1-0）/);
  assert.ok(run.logs.some((line) => line.startsWith("fail ") && line.includes("ack 超时（echo=req-1-0）")));
  assert.equal(ACK_TIMEOUT_MS, 10_000);
});

test("门关着：该泡与后面每泡各落一行 unsent，一个 send 都不发", async () => {
  const run = harness({ gateOpen: () => false });
  await runBatch(run.input);

  assert.deepEqual(echoes(run.events), []);
  assert.deepEqual(run.rows.map((row) => row.kind), ["unsent", "unsent", "unsent"]);
  assert.match(run.rows[0].note, /门已关/);
  assert.ok(run.logs.some((line) => line.startsWith("unsent ") && line.includes("门已关")));
});

test("发完第一泡后连接断了：后面全记 unsent", async () => {
  let open = true;
  const run = harness({ socketOpen: () => open });
  const waitFor = run.input.waitAck;
  run.input.waitAck = async (echo) => {
    const outcome = await waitFor(echo);
    open = false;
    return outcome;
  };
  await runBatch(run.input);

  assert.deepEqual(run.rows.map((row) => row.kind), ["sent", "unsent", "unsent"]);
  assert.match(run.rows[1].note, /连接已断/);
});

test("等 ack 时连接断开：记 uncertain 而不是假装成功", async () => {
  const run = harness({ acks: [{ kind: "closed" }] });
  await runBatch(run.input);

  assert.deepEqual(run.rows.map((row) => row.kind), ["uncertain", "unsent", "unsent"]);
  assert.match(run.rows[0].note, /连接断开/);
});

// settleRound 的假件：复用 runBatch 的那套，再补上「这一轮核发生了什么」。
const settleHarness = (options: { mode?: Mode; attempt: CoreAttempt; scene?: Scene; acks?: AckOutcome[] }) => {
  const base = harness({ mode: options.mode, acks: options.acks });
  const input: SettleInput = {
    mode: base.input.mode,
    batch: base.input.batch,
    where: base.input.where,
    scene: options.scene ?? "group",
    attempt: options.attempt,
    origin: 0,
    requestSeq: 1,
    elapsed: () => "0ms",
    ledger: base.input.ledger,
    isGateOpen: base.input.isGateOpen,
    isSocketOpen: base.input.isSocketOpen,
    send: base.input.send,
    waitAck: base.input.waitAck,
    sleep: base.input.sleep,
    now: base.input.now,
    log: base.input.log,
  };
  return { input, events: base.events, logs: base.logs, rows: base.rows };
};

test("settleRound：chunks=0 → 恰好 1 行 empty，0 次 send（silent 与 send 都落）", async () => {
  for (const mode of ["silent", "send"] as Mode[]) {
    const run = settleHarness({ mode, attempt: { kind: "payload", payload: { ok: true, chunks: [] } } });
    const result = await settleRound(run.input);

    assert.equal(result.kind, "empty");
    assert.equal(run.rows.length, 1);
    assert.equal(run.rows[0].kind, "empty");
    assert.equal(run.rows[0].mode, mode);
    assert.equal(run.rows[0].text, null);
    assert.equal(run.rows[0].index, null);
    assert.equal(run.rows[0].send_at, null);
    assert.equal(run.rows[0].note, "chunks=0（她这轮没说）");
    assert.deepEqual(echoes(run.events), []);
    assert.ok(run.logs.some((line) => line.startsWith("plan ") && line.includes("chunks=0（她这轮没说）")));
  }
});

test("settleRound：核不可达 → 恰好 1 行 error，note 前缀 核不可达：", async () => {
  const run = settleHarness({ attempt: { kind: "unreachable", message: "fetch failed: ECONNREFUSED" } });
  const result = await settleRound(run.input);

  assert.equal(result.kind, "error");
  assert.equal(run.rows.length, 1);
  assert.equal(run.rows[0].kind, "error");
  assert.equal(run.rows[0].note, "核不可达：fetch failed: ECONNREFUSED");
  assert.equal(run.rows[0].retcode, null);
  assert.equal(run.rows[0].status, null);
  assert.equal(run.rows[0].text, null);
  assert.deepEqual(echoes(run.events), []);
  assert.ok(run.logs.some((line) => line.startsWith("error ")));
});

test("settleRound：HTTP 非 2xx → 恰好 1 行 error，note 带状态码与响应前 200 字", async () => {
  const run = settleHarness({ attempt: { kind: "http", status: 500, body: "x".repeat(300) } });
  const result = await settleRound(run.input);

  assert.equal(result.kind, "error");
  assert.equal(run.rows.length, 1);
  assert.equal(run.rows[0].kind, "error");
  assert.equal(run.rows[0].note, `核 HTTP 500：${"x".repeat(200)}`);
  assert.deepEqual(echoes(run.events), []);
});

test("settleRound：响应认不出 → 恰好 1 行 error，note 前缀 响应认不出：", async () => {
  for (const attempt of [
    { kind: "payload", payload: { ok: false, error: "闸住了" } } as CoreAttempt,
    { kind: "payload", payload: { ok: true } } as CoreAttempt,
    { kind: "unreadable", reason: "不是合法 JSON：Unexpected token <" } as CoreAttempt,
  ]) {
    const run = settleHarness({ attempt });
    await settleRound(run.input);
    assert.equal(run.rows.length, 1);
    assert.equal(run.rows[0].kind, "error");
    assert.match(run.rows[0].note, /^响应认不出：/);
    assert.deepEqual(echoes(run.events), []);
  }
});

test("settleRound：scene=unknown 时核回了 chunks 也不许 send，只落 1 行 error", async () => {
  const run = settleHarness({
    scene: "unknown",
    attempt: { kind: "payload", payload: { ok: true, chunks: [{ text: "不该发出去", gap_before: 0, send_at: 0 }] } },
  });
  const result = await settleRound(run.input);

  assert.equal(result.kind, "error");
  assert.deepEqual(run.events, []);
  assert.deepEqual(echoes(run.events), []);
  assert.equal(run.rows.length, 1);
  assert.equal(run.rows[0].kind, "error");
  assert.equal(run.rows[0].note, "scene 认不出（unknown），不发");
  assert.equal(run.rows[0].target, "group:700000001", "账上的目标仍按 batch 记录，没被污染");
});

test("settleRound：四种 error 的 note 互不歧义", async () => {
  const cases: Array<{ attempt: CoreAttempt; scene?: Scene; prefix: string }> = [
    { attempt: { kind: "unreachable", message: "boom" }, prefix: "核不可达：" },
    { attempt: { kind: "http", status: 502, body: "bad gateway" }, prefix: "核 HTTP 502：" },
    { attempt: { kind: "unreadable", reason: "不是合法 JSON：x" }, prefix: "响应认不出：" },
    { attempt: { kind: "payload", payload: { ok: true } }, scene: "unknown", prefix: "scene 认不出（unknown），不发" },
  ];
  const notes: string[] = [];
  for (const item of cases) {
    const run = settleHarness({ attempt: item.attempt, scene: item.scene });
    await settleRound(run.input);
    assert.equal(run.rows.length, 1);
    assert.ok(run.rows[0].note.startsWith(item.prefix), `${run.rows[0].note} 应以 ${item.prefix} 开头`);
    assert.deepEqual(echoes(run.events), []);
    notes.push(run.rows[0].note);
  }
  assert.equal(new Set(notes.map((note) => note.split("：")[0])).size, 4, "四类前缀必须互不相同");
});

test("settleRound：silent 有 chunks → 仍是 1 行 blocked，0 次 send（这条别改坏）", async () => {
  const run = settleHarness({
    mode: "silent",
    attempt: { kind: "payload", payload: { ok: true, chunks: [{ text: "想说的", gap_before: 0, send_at: 0 }] } },
  });
  const result = await settleRound(run.input);

  assert.equal(result.kind, "blocked");
  assert.deepEqual(run.events, []);
  assert.equal(run.rows.length, 1);
  assert.equal(run.rows[0].kind, "blocked");
  assert.equal(run.rows[0].text, "想说的");
  assert.equal(run.rows[0].note, "chunks=1 send_at=[0]");
});

test("settleRound：send 有 chunks → 每泡一行 sent，echo 从 req-1-0 起", async () => {
  const run = settleHarness({
    mode: "send",
    attempt: { kind: "payload", payload: { ok: true, chunks: [{ text: "甲", gap_before: 0, send_at: 0 }, { text: "乙", gap_before: 1, send_at: 1 }] } },
  });
  const result = await settleRound(run.input);

  assert.equal(result.kind, "ran");
  assert.deepEqual(echoes(run.events), ["req-1-0", "req-1-1"]);
  assert.deepEqual(run.rows.map((row) => [row.kind, row.index]), [["sent", 0], ["sent", 1]]);
});

const tmpLedgerPath = (): string => join(mkdtempSync(join(tmpdir(), "onebot-claim-")), LEDGER_RELATIVE_PATH);

const readLedgerRows = (path: string): Array<Record<string, unknown>> =>
  readFileSync(path, "utf8")
    .split("\n")
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line));

test("在飞重投：claim 未落账期间第二次进来只落 1 行 dup，note 说清在飞", async () => {
  const path = tmpLedgerPath();
  const ledger = new Ledger({ path });
  const run = harness({ mode: "silent" });
  const key = run.input.batch.key;

  assert.equal(ledger.claim(key), true);
  // 模拟 300ms 后的同一条重投：第一批还在等核/等 ack，账里一行都没有。
  assert.equal(ledger.claim(key), false);
  const state = ledger.firstKind(key);
  assert.equal(state, "claimed");
  const note = dedupNote(state);
  assert.match(note, /在飞/);
  ledger.appendNew({ kind: "dup", mode: "silent", key, messageId: run.input.batch.messageId, scene: run.input.batch.scene, target: run.input.batch.target, note });

  // 第一批照常走完：claim 本身不落行，silent 仍只补 1 行 blocked。
  await runBatch({ ...run.input, ledger });
  ledger.release(key);

  assert.deepEqual(readLedgerRows(path).map((row) => row.kind), ["dup", "blocked"]);
  assert.equal(ledger.firstKind(key), "blocked", "真行优先，不该还报 claimed/dup");
});

test("claim 之后行数不变：silent 恰好 1 行 blocked，send 按泡 N 行", async () => {
  const silentPath = tmpLedgerPath();
  const silentLedger = new Ledger({ path: silentPath });
  const silentRun = harness({ mode: "silent" });
  assert.equal(silentLedger.claim(silentRun.input.batch.key), true);
  await runBatch({ ...silentRun.input, ledger: silentLedger });
  silentLedger.release(silentRun.input.batch.key);
  assert.deepEqual(readLedgerRows(silentPath).map((row) => row.kind), ["blocked"]);

  const sendPath = tmpLedgerPath();
  const sendLedger = new Ledger({ path: sendPath });
  const sendRun = harness({ mode: "send" });
  assert.equal(sendLedger.claim(sendRun.input.batch.key), true);
  await runBatch({ ...sendRun.input, ledger: sendLedger });
  sendLedger.release(sendRun.input.batch.key);
  assert.deepEqual(readLedgerRows(sendPath).map((row) => row.kind), ["sent", "sent", "sent"]);
});

test("readCoreTimeout：默认 60000，给值生效，缺值/非数字/负数/0 都回落并留 warn", () => {
  assert.deepEqual(readCoreTimeout(null), { ms: CORE_TIMEOUT_MS, warn: null });
  assert.equal(CORE_TIMEOUT_MS, 60_000);
  assert.deepEqual(readCoreTimeout("1500"), { ms: 1500, warn: null });
  assert.equal(readCoreTimeout(" 2500 ").ms, 2500);

  for (const raw of ["", "   ", "abc", "-5", "0", "NaN", "1e999"]) {
    const decision = readCoreTimeout(raw);
    assert.equal(decision.ms, CORE_TIMEOUT_MS, `非法值 ${JSON.stringify(raw)} 必须回落默认`);
    assert.ok(decision.warn !== null, `非法值 ${JSON.stringify(raw)} 必须留一行 warn`);
    assert.match(decision.warn, /--core-timeout=/);
  }
});

test("settleRound：核超时 → 1 行 error，前缀是「核超时（Nms）：」而不是「核不可达：」", async () => {
  const run = settleHarness({ attempt: { kind: "timeout", timeoutMs: 1000, message: "The operation was aborted due to timeout" } });
  const result = await settleRound(run.input);

  assert.equal(result.kind, "error");
  assert.equal(run.rows.length, 1);
  assert.equal(run.rows[0].kind, "error");
  assert.equal(run.rows[0].note, "核超时（1000ms）：The operation was aborted due to timeout");
  assert.equal(run.rows[0].retcode, null);
  assert.deepEqual(run.events, [], "超时之后一个 send 都不许有");
  assert.ok(run.logs.some((line) => line.startsWith("error ") && line.includes("核超时（1000ms）")));
});

test("五种 error 前缀互不为前缀，可无歧义区分", async () => {
  const cases: Array<{ attempt: CoreAttempt; scene?: Scene; prefix: string }> = [
    { attempt: { kind: "unreachable", message: "fetch failed" }, prefix: "核不可达：" },
    { attempt: { kind: "timeout", timeoutMs: 1000, message: "aborted" }, prefix: "核超时（1000ms）：" },
    { attempt: { kind: "http", status: 502, body: "bad gateway" }, prefix: "核 HTTP 502：" },
    { attempt: { kind: "unreadable", reason: "不是合法 JSON：x" }, prefix: "响应认不出：" },
    { attempt: { kind: "payload", payload: { ok: true } }, scene: "unknown", prefix: "scene 认不出（unknown），不发" },
  ];
  const notes: string[] = [];
  for (const item of cases) {
    const run = settleHarness({ attempt: item.attempt, scene: item.scene });
    await settleRound(run.input);
    assert.equal(run.rows.length, 1);
    assert.ok(run.rows[0].note.startsWith(item.prefix), `${run.rows[0].note} 应以 ${item.prefix} 开头`);
    assert.deepEqual(echoes(run.events), []);
    notes.push(run.rows[0].note);
  }
  for (const a of notes) {
    for (const b of notes) {
      if (a !== b) assert.equal(a.startsWith(b), false, `前缀 ${a} 与 ${b} 不许互相包含`);
    }
  }
  assert.equal(notes.length, 5);
});
