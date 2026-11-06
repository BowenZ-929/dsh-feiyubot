import { test } from "node:test";
import assert from "node:assert/strict";
import { BatchHub } from "./batch.ts";
import type { BatchSnapshot, ObserveInput, Where } from "./batch.ts";
import { readPolicy, loadPolicy } from "./policy.ts";
import type { BatchPolicy, Policy } from "./policy.ts";
import { parseEvent } from "./parse.ts";
import type { ParsedEvent } from "./parse.ts";
import { renderSegment } from "./segment.ts";

const A = 3000000001;
const B = 3000000002;
const C = 3000000003;
const PU = 3000000009;
const GROUP: Where = { scene: "group", groupId: 700000001 };
const PRIVATE: Where = { scene: "private", userId: PU };
// 会话键与 gate.targetOf 同源：group:<id> / user:<id>
const K = "group:700000001";
const PK = "user:3000000009";
const NAMES: Record<number, string> = { [A]: "小明", [B]: "小美", [C]: "小刚", [PU]: "小雨" };

// 每份测试都从真实的 data/policy.json 出发再按需覆盖：断言里的数字才和落盘系数是同一套。
const policy = (): Policy => structuredClone(loadPolicy());

const hub = (patch: Partial<BatchPolicy> = {}): BatchHub => {
  const base = policy();
  return new BatchHub({ ...base.batch, ...patch });
};

const msg = (senderId: number, text: string, extra: Partial<ObserveInput> = {}): ObserveInput => ({
  senderId,
  senderName: NAMES[senderId] ?? String(senderId),
  text,
  mentioned: false,
  bareImage: false,
  sticker: false,
  where: GROUP,
  ...extra,
});

// 「行」是核真正读到的东西：段渲染之后每个物理行都带一个 speaker。
const linesOf = (snapshot: BatchSnapshot): string[] => renderSegment(snapshot).text.split("\n");

// ── 合批口径（冻结）─────────────────────────────────────────────────────────
// 判据一句话：合批的单位是「这一小段在聊什么」，不是「谁说了几句」。
// 开批：群聊 quietGapMs 之外 quietOpen(2s) / 之内 activeOpen(5s)；私聊恒 privateOpen(3.5s)。
// 续发：不分人，恒 continuing，群 3s / 私聊 3.5s。
// 封顶：firstAt + hardCapMs(8s)；本批被 @ 过再加 atFirst + mentionCapMs(1s)；满 maxMsgs(6) 条立刻出手。

test("A1 私聊连发 3 条、间隔 3s：1 段 3 行、speaker 同一人、tier=continuing，deadline 3500→6500→8000", () => {
  const h = hub();
  const tiers: Array<string | null> = [];
  const deadlines: Array<number | null> = [];
  for (const [index, now] of [0, 3000, 6000].entries()) {
    h.observe(msg(PU, `第${index + 1}句`, { where: PRIVATE }), now);
    tiers.push(h.state(PK).tier);
    deadlines.push(h.state(PK).deadline);
  }

  // 私聊不再「每条即批」：第一句开 3.5s 窗，后面每句把窗口推到自己 +3.5s，8s 硬顶封住。
  assert.deepEqual(tiers, ["privateOpen", "continuing", "continuing"]);
  assert.deepEqual(deadlines, [3500, 6500, 8000]);

  assert.equal(h.consume(PK, 7999), null); // 差 1ms 都不出手
  const batch = h.consume(PK, 8000);
  assert.ok(batch);
  assert.equal(batch.msgs.length, 3);
  assert.deepEqual(
    batch.msgs.map((m) => m.id),
    [PU, PU, PU],
  );
  assert.equal(batch.msgs[0].name, "小雨"); // speaker 同一人
  assert.equal(batch.tier, "continuing");
  assert.equal(batch.firstAt, 0);
  assert.equal(batch.lastAt, 6000);
  assert.equal(batch.waited_ms, 6000);
  assert.equal(batch.scene, "private");
  assert.equal(batch.userId, PU);
  assert.equal("groupId" in batch, false);
  const lines = linesOf(batch);
  assert.equal(lines.length, 3); // 1 段 3 行
  for (const line of lines) assert.ok(line.startsWith("小雨(3000000009)："), line);
  assert.equal(h.consume(PK, 8000), null);
});

test("A1b 单条私聊：开批等 3500ms 就出手，不是 0 延迟", () => {
  const h = hub();
  h.observe(msg(PU, "只有一句", { where: PRIVATE }), 0);
  assert.equal(h.state(PK).tier, "privateOpen");
  assert.equal(h.state(PK).deadline, 3500);
  assert.equal(h.consume(PK, 3499), null);
  const batch = h.consume(PK, 3500);
  assert.ok(batch);
  assert.equal(batch.msgs.length, 1);
  assert.equal(batch.waited_ms, 0);
});

test("A2 群里 3 个人、3 秒内共 6 条：1 段 6 行、speaker 三人、tier 走 continuing", () => {
  const h = hub();
  const script: Array<[number, number]> = [
    [0, A],
    [600, B],
    [1200, C],
    [1800, A],
    [2400, B],
    [3000, C],
  ];
  const tiers: Array<string | null> = [];
  const deadlines: Array<number | null> = [];
  for (const [now, senderId] of script) {
    h.observe(msg(senderId, `${now}ms 的话`), now);
    tiers.push(h.state(K).tier);
    deadlines.push(h.state(K).deadline);
  }

  assert.deepEqual(tiers, ["quietOpen", "continuing", "continuing", "continuing", "continuing", "continuing"]);
  // 前五条各自 3s 续窗；第 6 条落地即满 maxMsgs，deadline 压到它自己的到达时刻 3000。
  assert.deepEqual(deadlines, [2000, 3600, 4200, 4800, 5400, 3000]);

  assert.equal(h.consume(K, 2999), null);
  const batch = h.consume(K, 3000);
  assert.ok(batch);
  assert.equal(batch.msgs.length, 6);
  assert.deepEqual(
    batch.msgs.map((m) => m.id),
    [A, B, C, A, B, C],
  );
  const lines = linesOf(batch);
  assert.equal(lines.length, 6); // 1 段 6 行
  assert.deepEqual([...new Set(lines.map((line) => line.split("(")[0]))].sort(), ["小刚", "小明", "小美"]);
  assert.equal(batch.tier, "continuing");
});

test("A3 群里换人但间隔 1s（不同人交替）：仍是 1 段 4 条 —— 换人不再断开", () => {
  const h = hub();
  const script: Array<[number, number]> = [
    [0, A],
    [1000, B],
    [2000, A],
    [3000, B],
  ];
  const tiers: Array<string | null> = [];
  const deadlines: Array<number | null> = [];
  for (const [now, senderId] of script) {
    h.observe(msg(senderId, `${now}ms 的话`), now);
    tiers.push(h.state(K).tier);
    deadlines.push(h.state(K).deadline);
  }

  assert.deepEqual(tiers, ["quietOpen", "continuing", "continuing", "continuing"]);
  assert.deepEqual(deadlines, [2000, 4000, 5000, 6000]);

  const batch = h.consume(K, 6000);
  assert.ok(batch);
  assert.equal(batch.msgs.length, 4);
  assert.deepEqual(
    batch.msgs.map((m) => m.id),
    [A, B, A, B],
  );
  assert.equal(batch.firstAt, 0);
  assert.equal(batch.lastAt, 3000);
  assert.equal(batch.tier, "continuing");
});

test("A4 被 @：deadline <= atFirst + 1000，且只认最早那一次（群 / 私聊都一样）", () => {
  const h = hub();
  h.observe(msg(A, "叫她一声", { mentioned: true }), 1000);
  assert.equal(h.state(K).atFirst, 1000);
  assert.equal(h.state(K).deadline, 2000); // min(1000+quietOpen2000, 1000+@1000) = 2000
  assert.ok((h.state(K).deadline ?? Infinity) <= 1000 + 1000);

  // 续发里补 @：3s 续窗被 "@ 那一刻 +1s" 压到 2000；再来一条 @ 不会改写 atFirst
  const late = hub();
  late.observe(msg(A, "一"), 0); // quietOpen → 2000
  late.observe(msg(A, "二", { mentioned: true }), 1000); // continuing 4000 → @ 上限 2000
  assert.equal(late.state(K).atFirst, 1000);
  assert.equal(late.state(K).deadline, 2000);
  late.observe(msg(B, "三", { mentioned: true }), 1500);
  assert.equal(late.state(K).atFirst, 1000); // atFirst 只 min 不改
  assert.equal(late.state(K).deadline, 2000);

  // 私聊里被 @：3500 窗口同样被 1s 封顶
  const priv = hub();
  priv.observe(msg(PU, "私聊被叫", { where: PRIVATE, mentioned: true }), 0);
  assert.equal(priv.state(PK).deadline, 1000);
  assert.ok((priv.state(PK).deadline ?? Infinity) <= 0 + 1000);

  // 已到期的批里补 @：只并入，deadline 一个字节都不动
  const expired = hub();
  expired.observe(msg(A, "一"), 0);
  expired.observe(msg(A, "二", { mentioned: true }), 2001);
  assert.equal(expired.state(K).deadline, 2000);
  assert.equal(expired.state(K).count, 2);
});

test("A5 连发 8 条、间隔 0.3s：两段 [6,2] —— 6 条上限真的生效，第 7 条绝不并入", () => {
  const h = hub();
  const tiers: Array<string | null> = [];
  const deadlines: Array<number | null> = [];
  const batchIds: Array<string | null> = [];
  for (let index = 0; index < 8; index += 1) {
    const now = index * 300;
    h.observe(msg(A, `第${index + 1}句`), now);
    tiers.push(h.state(K).tier);
    deadlines.push(h.state(K).deadline);
    batchIds.push(h.state(K).batchId);
  }

  assert.deepEqual(tiers, [
    "quietOpen",
    "continuing",
    "continuing",
    "continuing",
    "continuing",
    "continuing",
    "activeOpen",
    "continuing",
  ]);
  // 第 6 条（1500ms）满 6 → deadline = 1500；第 7 条到来时满段已被挪走，于是开了 b2（1800+5000=6800）。
  assert.deepEqual(deadlines, [2000, 3300, 3600, 3900, 4200, 1500, 6800, 5100]);
  assert.deepEqual(batchIds, ["b1", "b1", "b1", "b1", "b1", "b1", "b2", "b2"]);

  // 第 7 条落地那一刻：满段 b1 已在待取队列里，state 报的是新段 b2 —— 这就是"第 7 条不并入"的证据。
  assert.equal(h.state(K).queued, 1);
  assert.equal(h.state(K).count, 2);

  const first = h.consume(K, 5100);
  assert.ok(first);
  assert.equal(first.batchId, "b1");
  assert.equal(first.msgs.length, 6);
  assert.deepEqual(
    first.msgs.map((m) => m.text),
    ["第1句", "第2句", "第3句", "第4句", "第5句", "第6句"],
  );

  const second = h.consume(K, 5100);
  assert.ok(second);
  assert.equal(second.batchId, "b2");
  assert.equal(second.msgs.length, 2);
  assert.deepEqual(
    second.msgs.map((m) => m.text),
    ["第7句", "第8句"],
  );
  assert.equal(second.firstAt, 1800);
  assert.equal(second.lastAt, 2100);

  // FIFO：先取走的必须是 shelve 进去的那一段（b1），新开的那段（b2）排在后头——
  // 否则第 6 条会被第 7 条插队，账里 b1/b2 的顺序就反了。
  assert.deepEqual([first.batchId, second.batchId], ["b1", "b2"]);
  assert.equal(h.consume(K, 5100), null);
  assert.deepEqual([first.msgs.length, second.msgs.length], [6, 2]);
});

test("A5b FIFO：连发 13 条、间隔 0.3s → 两段满段先后入队、第三段还开着，取件顺序仍是 b1,b2,b3", () => {
  const h = hub();
  for (let index = 0; index < 13; index += 1) h.observe(msg(A, `第${index + 1}句`), index * 300);

  // 第 12 条落地时 b2 也满了 → 队列里是 [b1, b2]，第 13 条自己开了 b3
  assert.equal(h.state(K).batchId, "b3");
  assert.equal(h.state(K).queued, 2);
  assert.equal(h.state(K).count, 1);

  const segments = [
    h.consume(K, 8600),
    h.consume(K, 8600),
    h.consume(K, 8600),
  ];
  for (const segment of segments) assert.ok(segment);
  assert.deepEqual(
    segments.map((segment) => segment?.batchId),
    ["b1", "b2", "b3"], // 队列先于 open，队列内部先进先出
  );
  assert.deepEqual(
    segments.map((segment) => segment?.msgs.length),
    [6, 6, 1],
  );
  assert.deepEqual(
    segments.map((segment) => segment?.msgs[0]?.text),
    ["第1句", "第7句", "第13句"],
  );
  assert.equal(h.consume(K, 8600), null);
});

test("A6 硬顶：任何情况下 deadline <= firstAt + 8000，裸图/表情/@ 都逃不出去", () => {
  // 条数上限先放宽，好让这一批活到硬顶（其余系数仍是落盘那份）
  const h = hub({ maxMsgs: 20 });
  const deadlines: Array<number | null> = [];
  for (let now = 0; now <= 8000; now += 1000) {
    h.observe(msg(A, `${now}ms`), now);
    deadlines.push(h.state(K).deadline);
  }
  assert.deepEqual(deadlines, [2000, 4000, 5000, 6000, 7000, 8000, 8000, 8000, 8000]);

  const firstAt = h.state(K).firstAt;
  assert.equal(firstAt, 0);
  for (const deadline of deadlines) assert.ok((deadline ?? Infinity) <= (firstAt ?? 0) + 8000);

  // 裸图想多等 4s（8000+4000），硬顶一样把它按回 8000
  h.observe(msg(B, "[图片]", { bareImage: true }), 8000);
  assert.equal(h.state(K).deadline, 8000);
  assert.ok((h.state(K).deadline ?? Infinity) <= (firstAt ?? 0) + 8000);

  // 硬顶也压得住裸图的"抬"：注入 3000 硬顶 + 4000 裸图 → 出手仍是 firstAt+3000，不是 now+4000
  const injected = hub({ maxMsgs: 20, hardCapMs: 3000 });
  injected.observe(msg(A, "在吗"), 0);
  injected.observe(msg(B, "[图片]", { bareImage: true }), 2000);
  assert.equal(injected.state(K).deadline, 3000); // max(2000+3000, 2000+4000)=6000 → min(…, 0+3000)=3000
});

test("边界：now == deadline 仍可续（deadline 变大）；now == deadline+1 只并入、deadline 不动", () => {
  const alive = hub();
  alive.observe(msg(A, "第 1 句"), 0);
  assert.equal(alive.state(K).deadline, 2000);
  alive.observe(msg(A, "正好 2000 来的第 2 句"), 2000);
  assert.equal(alive.state(K).tier, "continuing");
  assert.equal(alive.state(K).deadline, 5000); // 2000 + continueMs.group 3000

  const late = hub();
  late.observe(msg(A, "第 1 句"), 0);
  late.observe(msg(A, "2001 才来的第 2 句"), 2001);
  assert.equal(late.state(K).deadline, 2000); // 只并入，一个字节都不延长
  assert.equal(late.state(K).count, 2);
  assert.equal(late.state(K).tier, "quietOpen"); // tier 描述上一次 deadline 怎么算的，跟着冻住

  const batch = late.consume(K, 2001);
  assert.ok(batch);
  assert.equal(batch.msgs.length, 2);
  assert.equal(batch.tier, "quietOpen");
});

test("裸图：开批至少等 4s，但抬不过硬顶，也不会把更长的 tier 压短", () => {
  const h = hub();
  h.observe(msg(A, "[图片]", { bareImage: true }), 0);
  assert.equal(h.state(K).deadline, 4000); // quietOpen 2000 被裸图抬到 4000
  assert.ok((h.state(K).deadline ?? 0) >= 0 + 4000);

  const lift = hub();
  lift.observe(msg(A, "在吗"), 0);
  lift.observe(msg(B, "[图片]", { bareImage: true }), 1000);
  assert.equal(lift.state(K).deadline, 5000); // 续窗 4000 → 抬到 1000+4000

  // 第 5 条（不是第 6 条，别撞上 maxMsgs 即出手）是裸图：now+4000=9000 仍被 firstAt+8000 压回去
  const capped = hub();
  for (let index = 0; index < 4; index += 1) capped.observe(msg(A, `第${index + 1}句`), index * 1000);
  capped.observe(msg(B, "[图片]", { bareImage: true }), 5000);
  assert.equal(capped.state(K).count, 5);
  assert.equal(capped.state(K).deadline, 8000);
});

test("纯表情：只能提前不能延后", () => {
  const h = hub();
  h.observe(msg(A, "一"), 0); // 2000
  h.observe(msg(A, "[表情:14]", { sticker: true }), 500); // 续窗 3500 → 表情压到 1000
  assert.equal(h.state(K).deadline, 1000);
  assert.ok(1000 < 3500);

  const fresh = hub();
  fresh.observe(msg(A, "[表情:1]", { sticker: true }), 0);
  assert.equal(fresh.state(K).deadline, 500); // 开批就是表情：500ms 就出手
});

test("系数活读：openMs / continueMs / hardCapMs / maxMsgs 都由注入的 policy 说话", () => {
  const h = hub({
    hardCapMs: 12000,
    openMs: { quietOpen: 5000, activeOpen: 5000, privateOpen: 3500 },
    continueMs: { group: 900, private: 3500 },
  });
  h.observe(msg(A, "一"), 0);
  assert.equal(h.state(K).deadline, 5000); // 注入的 quietOpen
  h.observe(msg(A, "二"), 100);
  assert.equal(h.state(K).deadline, 1000); // 100 + 注入的 900
  h.observe(msg(A, "三"), 700);
  assert.equal(h.state(K).deadline, 1600); // 700 + 900，硬顶 12000 不参与

  const wide = hub({ maxMsgs: 2 });
  wide.observe(msg(A, "一"), 0);
  wide.observe(msg(A, "二"), 100);
  assert.equal(wide.state(K).count, 2);
  assert.equal(wide.state(K).deadline, 100); // 满 2 条即压到到达时刻
  wide.observe(msg(A, "三"), 200);
  assert.equal(wide.state(K).batchId, "b2"); // 满段先取走，第 3 条开新段
  assert.equal(wide.state(K).queued, 1);
  const first = wide.consume(K, 200);
  const second = wide.consume(K, 5200);
  assert.ok(first && second);
  assert.deepEqual([first.msgs.length, second.msgs.length], [2, 1]);
});

test("会话隔离：A、B 两群交替各说 2 句 → 恰好 2 段，每段只含本群消息、where 各自正确", () => {
  const GA: Where = { scene: "group", groupId: 811111111 };
  const GB: Where = { scene: "group", groupId: 822222222 };
  const KA = "group:811111111";
  const KB = "group:822222222";
  const h = hub();
  const script: Array<[number, Where, number]> = [
    [0, GA, A],
    [300, GB, B],
    [600, GA, A],
    [900, GB, B],
  ];
  for (const [now, where, senderId] of script) {
    h.observe({ ...msg(senderId, `${where === GA ? "A 群" : "B 群"} ${now}`), where }, now);
  }

  assert.equal(h.state(KA).deadline, 3600); // 600 + 3000
  assert.equal(h.state(KB).deadline, 3900); // 900 + 3000
  assert.deepEqual(h.openKeys().sort(), [KA, KB].sort());

  const a = h.consume(KA, 3600);
  const b = h.consume(KB, 3900);
  assert.ok(a && b);
  assert.equal(a.msgs.length, 2);
  assert.equal(b.msgs.length, 2);
  assert.deepEqual(a.msgs.map((m) => m.text), ["A 群 0", "A 群 600"]);
  assert.deepEqual(b.msgs.map((m) => m.text), ["B 群 300", "B 群 900"]);
  assert.equal(a.groupId, 811111111);
  assert.equal(b.groupId, 822222222);
  assert.equal(a.scene, "group");
  assert.equal(h.consume(KA, 4000), null);
  assert.equal(h.consume(KB, 4000), null);
  assert.deepEqual(h.openKeys(), []);
});

test("会话级安静阈值：B 群刚热闹过，A 群 31 秒没说话 → A 群开批仍是 quietOpen", () => {
  const GA: Where = { scene: "group", groupId: 811111111 };
  const GB: Where = { scene: "group", groupId: 822222222 };
  const KA = "group:811111111";
  const KB = "group:822222222";
  const h = hub();

  h.observe({ ...msg(A, "A 群第 1 句"), where: GA }, 0);
  h.consume(KA, 2000); // 让 A 群先安静下来（A 的「上一条到达」仍是 0）

  // B 群 1s / 2s / 3s 各一句（每段都取走），把 B 的「上一条到达」推到 3s——但只属于 B
  h.observe({ ...msg(B, "B 群 1s"), where: GB }, 1000);
  h.consume(KB, 6000);
  h.observe({ ...msg(B, "B 群 2s"), where: GB }, 2000);
  h.consume(KB, 7000);
  h.observe({ ...msg(B, "B 群 3s"), where: GB }, 3000);
  h.consume(KB, 8000);

  // A 群 31 秒后才说话：A 自己的上一条是 0ms → 31s > quietGapMs(30s) → 安静档。
  h.observe({ ...msg(A, "A 群 31 秒后"), where: GA }, 31000);
  assert.equal(h.state(KA).tier, "quietOpen");
  assert.equal(h.state(KA).deadline, 33000); // 31s + quietOpen 2000

  // 对照：B 群自己上一句是 3s，31s 只隔 28s ≤ 30s → 活跃档
  h.observe({ ...msg(B, "B 群 31 秒"), where: GB }, 31000);
  assert.equal(h.state(KB).tier, "activeOpen");
  assert.equal(h.state(KB).deadline, 36000); // 31s + activeOpen 5000
});

// parse 的三个判定（@ 我 / 裸图 / 纯表情）是合批的唯一入口：这里让真实事件走完整条链路。
const groupEvent = (message: unknown[], extra: Record<string, unknown> = {}) => ({
  post_type: "message",
  message_type: "group",
  group_id: 700000001,
  message_id: 2000000001,
  time: 1761523200,
  self_id: 1000000001,
  sender: { user_id: A, nickname: "小明" },
  message,
  ...extra,
});

const textSeg = (text: string) => ({ type: "text", data: { text } });
const atSeg = (qq: string) => ({ type: "at", data: { qq } });

const expectParsed = (raw: unknown): ParsedEvent => {
  const parsed = parseEvent(raw);
  assert.equal(parsed.ok, true, parsed.ok ? "" : `意外失败：${parsed.reason}`);
  if (!parsed.ok) throw new Error("unreachable");
  return parsed;
};

test("parse 的判定直接喂进合批：@我 / 裸图 / 纯表情", () => {
  const atMe = expectParsed(groupEvent([atSeg("1000000001"), textSeg(" 在吗")]));
  assert.equal(atMe.mentionedSelf, true);
  assert.equal(atMe.mentionedAll, false);
  assert.equal(atMe.isBareImage, false);
  assert.equal(atMe.isSticker, false);

  const bareImage = expectParsed(groupEvent([{ type: "image", data: { file: "ab12cd34.image" } }]));
  assert.equal(bareImage.isBareImage, true);
  assert.equal(bareImage.hasTextSegment, false);

  const sticker = expectParsed(groupEvent([{ type: "mface", data: { summary: "[动画表情]" } }]));
  assert.equal(sticker.isSticker, true);
  assert.equal(sticker.isBareImage, false);

  const fromEvent = (event: ParsedEvent): ObserveInput => ({
    senderId: event.who.id,
    senderName: event.who.card ?? event.who.nickname,
    text: event.text,
    mentioned: event.mentionedSelf,
    bareImage: event.isBareImage,
    sticker: event.isSticker,
    where: event.where,
  });

  const mentionedHub = hub();
  mentionedHub.observe(fromEvent(atMe), 0);
  assert.equal(mentionedHub.state(K).deadline, 1000); // @ 上限 1000 < quietOpen 2000

  const imageHub = hub();
  imageHub.observe(fromEvent(bareImage), 0);
  assert.equal(imageHub.state(K).deadline, 4000);

  const stickerHub = hub();
  stickerHub.observe(fromEvent(sticker), 0);
  assert.equal(stickerHub.state(K).deadline, 500);
});

// policy.ts 的删键守门：删掉的键不许静默被忽略，哪怕它只是静静地躺在文件里。
test("policy 旧键：tiersMs / privateCapMs 还在文件里 → 读盘这一刻就给人话错误", () => {
  const good = () => structuredClone(loadPolicy()) as unknown as Record<string, any>;

  const withTiers = good();
  withTiers.batch.tiersMs = { quietOpen: 2000, activeOpen: 5000, sameSpeaker: 2000, switchSpeaker: 500 };
  assert.throws(() => readPolicy(withTiers), /batch.tiersMs 已经删掉了.*openMs.quietOpen/s);

  const withPrivateCap = good();
  withPrivateCap.batch.privateCapMs = 1000;
  assert.throws(() => readPolicy(withPrivateCap), /batch.privateCapMs 已经删掉了.*continueMs.private/s);

  // 对照：新键齐全时不炸
  assert.deepEqual(readPolicy(JSON.parse(JSON.stringify(loadPolicy()))), loadPolicy());
});
