import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  DECISIONS_RELATIVE_PATH,
  DecisionLog,
  decidedOf,
  humanOf,
  makeDecision,
  readGateSummary,
} from "./decide.ts";
import type { DecisionInput, GateInput } from "./decide.ts";

const policy = {
  decide: {
    reasonCodes: [
      "sent",
      "silent_mode",
      "quota_exceeded",
      "core_error",
      "outbound_failed",
      "core_empty",
      "replaced_too_long",
      "command",
      "sticker",
      "media_only",
      "burst_guard",
      "chitchat_skipped",
    ],
  },
};

const freshPath = (): string => join(mkdtempSync(join(tmpdir(), "onebot-decide-")), DECISIONS_RELATIVE_PATH);

const batch = (id: string) => ({ id, msgs: 2, waited_ms: 1500, tier: "sameSpeaker", at: "2026-05-06T10:00:00.000Z" });

const gate = (name: string, code: string, hit: boolean): GateInput => ({ name, code, hit });

// 门的顺序就是判定顺序：前面的先判，第一个 hit 的就是拦路那道。
const input = (over: Partial<DecisionInput> = {}): DecisionInput => ({
  scene: "group",
  mentioned: false,
  target: "group:700000001",
  batch: batch("b1"),
  gates: [gate("silent", "silent_mode", false), gate("direct", "chitchat_skipped", false)],
  ...over,
});

const readLines = (path: string): string[] =>
  readFileSync(path, "utf8").split("\n").filter((line) => line.trim().length > 0);

test("决策：每批恰好一行，字段顺序固定", () => {
  const path = freshPath();
  const log = new DecisionLog({ path, policy, now: () => new Date("2026-05-06T10:00:01.000Z") });
  log.append(input());
  log.append(input({ batch: batch("b2") }));
  const lines = readLines(path);
  assert.equal(lines.length, 2, "两批就是两行，没回的批次也不许省");
  const row = JSON.parse(lines[0]);
  assert.deepEqual(Object.keys(row), ["ts", "kind", "scene", "human", "machine"]);
  assert.deepEqual(Object.keys(row.machine), [
    "batch",
    "gates",
    "decided",
    "why",
    "reason_code",
    "scene",
    "target",
    "reason",
    "source",
    "gate",
    "direct",
  ]);
  assert.deepEqual(Object.keys(row.machine.batch), ["id", "msgs", "waited_ms", "tier", "at"]);
  assert.equal(row.kind, "decision");
  assert.equal(row.ts, "2026-05-06T10:00:01.000Z");
});

test("决策：九种结局的人话与 decided 都对应得上", () => {
  const cases: Array<{ name: string; input: DecisionInput; code: string; human: string; decided: string }> = [
    {
      name: "sent+@",
      input: input({ mentioned: true, gates: [] }),
      code: "sent",
      human: "回了：被 @",
      decided: "sent",
    },
    {
      name: "sent 普通",
      input: input({ gates: [gate("silent", "silent_mode", false)] }),
      code: "sent",
      human: "回了：接话",
      decided: "sent",
    },
    {
      name: "replaced_too_long",
      input: input({ gates: [gate("limits", "replaced_too_long", true)] }),
      code: "replaced_too_long",
      human: "回了：本来太长，换成一句短的",
      decided: "sent",
    },
    {
      name: "silent_mode",
      input: input({ gates: [gate("silent", "silent_mode", true)] }),
      code: "silent_mode",
      human: "没回：门关着（silent）",
      decided: "silent",
    },
    {
      name: "quota_exceeded",
      input: input({ gates: [gate("direct", "chitchat_skipped", false), gate("quota", "quota_exceeded", true)] }),
      code: "quota_exceeded",
      human: "没回：这个群这个小时说得够多了",
      decided: "silent",
    },
    {
      name: "core_empty",
      input: input({ mentioned: true, gates: [gate("core", "core_empty", true)] }),
      code: "core_empty",
      human: "没回：她这轮不想说",
      decided: "silent",
    },
    {
      name: "core_error",
      input: input({ gates: [gate("core", "core_error", true)] }),
      code: "core_error",
      human: "没回：核出了问题",
      decided: "silent",
    },
    {
      name: "outbound_failed",
      input: input({ gates: [gate("outbound", "outbound_failed", true)] }),
      code: "outbound_failed",
      // 措辞必须是「没确认」：ack 超时 / 连接断开时我们并不知道对方收没收到。
      human: "没回：发出去了但没确认（协议端没回执）",
      decided: "silent",
    },
    {
      name: "chitchat_skipped",
      input: input({ gates: [gate("silent", "silent_mode", false), gate("direct", "chitchat_skipped", true)] }),
      code: "chitchat_skipped",
      human: "没回：闲聊，没抽到",
      decided: "silent",
    },
  ];
  const path = freshPath();
  const log = new DecisionLog({ path, policy });
  cases.forEach((item, index) => {
    const row = log.append({ ...item.input, batch: batch(`b${index + 1}`) });
    assert.equal(row.machine.reason_code, item.code, item.name);
    assert.equal(row.human, item.human, item.name);
    assert.equal(row.machine.decided, item.decided, item.name);
    assert.equal(row.machine.scene, "group");
    assert.ok(policy.decide.reasonCodes.includes(row.machine.reason_code), "code 必须落在 policy 里");
  });
  assert.equal(readLines(path).length, cases.length, "九批就是九行");
  assert.equal(humanOf("silent_mode", true), "没回：门关着（silent）", "没回的人话不该被 mentioned 改写");
  assert.equal(decidedOf("replaced_too_long"), "sent");
});

test("决策：gates 记下所有判过的门（name+hit），顺序即判定顺序，拦路那道看得出", () => {
  const row = makeDecision(
    input({
      gates: [
        gate("silent", "silent_mode", false),
        gate("direct", "chitchat_skipped", false),
        gate("quota", "quota_exceeded", true),
        gate("core", "core_empty", false),
      ],
    }),
    "2026-05-06T10:00:00.000Z",
    policy.decide.reasonCodes,
  );
  assert.deepEqual(row.machine.gates, [
    { name: "silent", hit: false },
    { name: "direct", hit: false },
    { name: "quota", hit: true },
    { name: "core", hit: false },
  ]);
  assert.deepEqual(Object.keys(row.machine.gates[2]), ["name", "hit"], "门记录只许留 name/hit");
  assert.match(row.machine.why, /第 3 道门 quota 拦下了/);
});

test("决策：没有门拦时 why 说门全过；替换批次 why 说上限超了", () => {
  const passed = makeDecision(input({ gates: [gate("silent", "silent_mode", false)] }), "t", policy.decide.reasonCodes);
  assert.match(passed.machine.why, /门全过（1 道）/);
  const replaced = makeDecision(
    input({ gates: [gate("limits", "replaced_too_long", true)] }),
    "t",
    policy.decide.reasonCodes,
  );
  assert.match(replaced.machine.why, /三道上限超了/);
});

test("闸判摘要：readGateSummary 原样搬运，四种形状各一条", () => {
  // ① 命中：整轮摘要原样进来，字段一个不少、一个不改
  const hit = {
    at: "2026-11-06T05:00:00.000Z",
    audit: { status: "ok", hit: true, lcs_len: 42, threshold: 0.8 },
    rewrite: { status: "ok" },
  };
  assert.deepEqual(readGateSummary({ gate: { summary: hit } }), hit);
  assert.equal((readGateSummary({ gate: { summary: hit } }) as any).audit.lcs_len, 42);

  // ② 没这一格：回包里根本没有 gate（或 gate 里没有 summary）→ null，绝不造空壳
  assert.equal(readGateSummary({ ok: true, chunks: [] }), null);
  assert.equal(readGateSummary({ gate: {} }), null);
  assert.equal(readGateSummary({ gate: { summary: undefined } }), null);

  // ③ 垃圾形状：字符串 / 数组 / null 一律 null（不搬运垃圾，也不包成 {status:…}）
  assert.equal(readGateSummary({ gate: { summary: "hit" } }), null);
  assert.equal(readGateSummary({ gate: { summary: [1, 2, 3] } }), null);
  assert.equal(readGateSummary({ gate: { summary: null } }), null);

  // ④ payload 本身不是对象（null / 数组 / 字符串）→ null
  assert.equal(readGateSummary(null), null);
  assert.equal(readGateSummary([{ gate: { summary: hit } }]), null);
  assert.equal(readGateSummary("gate"), null);
});

test("决策：machine.gate 缺省是 null（账里必须看得见 \"gate\":null），给了就原样进账", () => {
  const plain = makeDecision(input(), "t", policy.decide.reasonCodes);
  assert.equal(plain.machine.gate, null);
  assert.match(JSON.stringify(plain), /"gate":null/);

  const summary = { at: "t", audit: { status: "ok", hit: true, lcs_len: 42, threshold: 0.8 }, rewrite: { status: "ok" } };
  const withGate = makeDecision(input({ gate: summary }), "t", policy.decide.reasonCodes);
  assert.deepEqual(withGate.machine.gate, summary);
  // gates（通道门链）与 gate（核的闸判）是两格，互不覆盖
  assert.deepEqual(withGate.machine.gates, [
    { name: "silent", hit: false },
    { name: "direct", hit: false },
  ]);

  assert.throws(
    () => makeDecision(input({ gate: "hit" as unknown as Record<string, unknown> }), "t", policy.decide.reasonCodes),
    /machine.gate 必须是 JSON 对象或 null/,
  );
});

test("决策：machine.direct 是 {rule, ordinal}，缺省是「没判据」", () => {
  const plain = makeDecision(input(), "t", policy.decide.reasonCodes);
  assert.deepEqual(plain.machine.direct, { rule: null, ordinal: null });

  const directed = makeDecision(input({ direct: { rule: "at", ordinal: 10 } }), "t", policy.decide.reasonCodes);
  assert.deepEqual(directed.machine.direct, { rule: "at", ordinal: 10 });

  // 拦下时 rule=null，ordinal 照记（群段序号对排障有用）
  const blocked = makeDecision(input({ direct: { rule: null, ordinal: 7 } }), "t", policy.decide.reasonCodes);
  assert.deepEqual(blocked.machine.direct, { rule: null, ordinal: 7 });

  // 垃圾值一律收敛成 null，绝不把字符串/对象写进账
  const janky = makeDecision(
    input({ direct: { rule: 42 as unknown as string, ordinal: "10" as unknown as number } }),
    "t",
    policy.decide.reasonCodes,
  );
  assert.deepEqual(janky.machine.direct, { rule: null, ordinal: null });
});

test("决策：source 必须显式区分入站与主动开口，缺省是 inbound", () => {
  const inbound = makeDecision(input(), "t", policy.decide.reasonCodes);
  assert.equal(inbound.machine.source, "inbound", "缺省必须是 inbound：入站调用点一行不改也得对");
  const proactive = makeDecision(input({ source: "proactive" }), "t", policy.decide.reasonCodes);
  assert.equal(proactive.machine.source, "proactive");
  assert.throws(
    () => makeDecision(input({ source: "cron" as unknown as "inbound" }), "t", policy.decide.reasonCodes),
    /machine.source 只认 inbound/,
  );
});

test("决策：target 原样进账；reason 只收非空字符串，why 里不许出现理由", () => {
  const withReason = makeDecision(
    input({ target: "user:3000000003", reason: "  刚在忙  " }),
    "t",
    policy.decide.reasonCodes,
  );
  assert.equal(withReason.machine.target, "user:3000000003");
  assert.equal(withReason.machine.reason, "刚在忙");
  // 理由只能走 machine.reason：塞进门名那种写法会让面板把它当成一道门。
  assert.equal(withReason.machine.why, "门全过（2 道），照发");

  const empty = makeDecision(input({ reason: "   " }), "t", policy.decide.reasonCodes);
  assert.equal(empty.machine.reason, null);
  const janky = makeDecision(input({ reason: { text: "刚在忙" } as unknown as string }), "t", policy.decide.reasonCodes);
  assert.equal(janky.machine.reason, null);
  assert.throws(() => makeDecision(input({ target: "" }), "t", policy.decide.reasonCodes), /target/);
});

test("决策：batch 快照原样进账（面板靠它把行和批次对上）", () => {
  const row = makeDecision(
    input({ scene: "private", mentioned: true, batch: { id: "b7", msgs: 1, waited_ms: 0, tier: "privateCap", at: "2026-05-06T10:00:00.000Z" } }),
    "t",
    policy.decide.reasonCodes,
  );
  assert.deepEqual(row.machine.batch, { id: "b7", msgs: 1, waited_ms: 0, tier: "privateCap", at: "2026-05-06T10:00:00.000Z" });
  assert.equal(row.scene, "private");
  assert.equal(row.machine.scene, "private");
  assert.equal(row.human, "回了：被 @");
});

test("决策：policy 之外的 reason_code 一律抛错，不许出现第四个值", () => {
  assert.throws(
    () => makeDecision(input({ gates: [gate("weird", "something_else", true)] }), "t", policy.decide.reasonCodes),
    /不在 policy.decide.reasonCodes/,
  );
  assert.throws(() => humanOf("botched", false), /没有 reason_code/);
  assert.throws(
    () => makeDecision(input({ batch: { id: "", msgs: 0, waited_ms: 0, tier: "t", at: "t" } }), "t", policy.decide.reasonCodes),
    /batch.id/,
  );
  // SegmentBatch.msgs 是消息数组，和这里的条数同名：接错时必须当场炸，不许把数组写进账。
  assert.throws(
    () =>
      makeDecision(
        input({ batch: { id: "b1", msgs: [{ id: 1 }] as unknown as number, waited_ms: 0, tier: "t", at: "t" } }),
        "t",
        policy.decide.reasonCodes,
      ),
    /必须是用条数|msgs 必须是条数/,
  );
});

test("决策：写不进去只 warn 不抛，行照常返回", () => {
  const dir = mkdtempSync(join(tmpdir(), "onebot-decide-bad-"));
  const warns: string[] = [];
  const log = new DecisionLog({ path: join(dir, "sub", "decisions.jsonl"), policy, onWarn: (m) => warns.push(m) });
  // 让目录变成"已被同名文件占住"，mkdir 必然失败。
  writeFileSync(join(dir, "sub"), "x", "utf8");
  const row = log.append(input());
  assert.equal(row.machine.reason_code, "sent");
  assert.equal(warns.length, 1);
  assert.match(warns[0], /决策账写不进去/);
});
