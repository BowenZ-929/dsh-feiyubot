import { test } from "node:test";
import assert from "node:assert/strict";
import { BatchHub } from "./batch.ts";
import type { ObserveInput, Where } from "./batch.ts";
import { loadPolicy } from "./policy.ts";
import type { BatchPolicy, Policy } from "./policy.ts";
import { parseEvent } from "./parse.ts";
import type { ParsedEvent } from "./parse.ts";

const A = 3000000001;
const B = 3000000002;
const GROUP: Where = { scene: "group", groupId: 700000001 };
const PRIVATE: Where = { scene: "private", userId: 3000000003 };
// 会话键与 gate.targetOf 同源：group:<id> / user:<id>
const K = "group:700000001";
const PK = "user:3000000003";

// 每份测试都从真实的 data/policy.json 出发再按需覆盖：断言里的数字才和落盘系数是同一套。
const policy = (): Policy => structuredClone(loadPolicy());

const hub = (patch: Partial<BatchPolicy> = {}): BatchHub => {
  const base = policy();
  return new BatchHub({ ...base.batch, ...patch });
};

const msg = (senderId: number, text: string, extra: Partial<ObserveInput> = {}): ObserveInput => ({
  senderId,
  senderName: senderId === A ? "小明" : "小美",
  text,
  mentioned: false,
  bareImage: false,
  sticker: false,
  where: GROUP,
  ...extra,
});

test("同人续发（间隔 500ms）：tier 序列与 2s 用例一致，deadline 逐条 +2s", () => {
  const h = hub();
  const tiers: Array<string | null> = [];
  const deadlines: Array<number | null> = [];
  for (const [index, now] of [0, 500, 1000].entries()) {
    h.observe(msg(A, `第${index + 1}句`), now);
    tiers.push(h.state(K).tier);
    deadlines.push(h.state(K).deadline);
  }
  assert.deepEqual(tiers, ["quietOpen", "sameSpeaker", "sameSpeaker"]);
  assert.deepEqual(deadlines, [2000, 2500, 3000]);
});

test("① 同人连发 3 句、间隔 2s：一批 3 条，tier 走 sameSpeaker，deadline 2000→4000→6000", () => {
  const h = hub();
  const tiers: Array<string | null> = [];
  const deadlines: Array<number | null> = [];
  for (const [index, now] of [0, 2000, 4000].entries()) {
    h.observe(msg(A, `第${index + 1}句`), now);
    tiers.push(h.state(K).tier);
    deadlines.push(h.state(K).deadline);
  }

  // now == deadline 那一刻仍算活着：2000 来的那条把批次续到 4000，4000 来的再续到 6000。
  assert.deepEqual(tiers, ["quietOpen", "sameSpeaker", "sameSpeaker"]);
  assert.deepEqual(deadlines, [2000, 4000, 6000]);

  assert.equal(h.consume(K, 5999), null);
  const batch = h.consume(K, 6000);
  assert.ok(batch);
  assert.equal(batch.msgs.length, 3);
  assert.deepEqual(
    batch.msgs.map((m) => m.text),
    ["第1句", "第2句", "第3句"],
  );
  assert.deepEqual(batch.msgs.map((m) => m.id), [A, A, A]);
  assert.equal(batch.firstAt, 0);
  assert.equal(batch.lastAt, 4000);
  assert.equal(batch.waited_ms, 4000);
  assert.equal(batch.tier, "sameSpeaker");
  assert.equal(h.consume(K, 6000), null);
});

test("②a A、B 交替 6 条、间隔 0.3s：一批 6 条，tier 出现 switchSpeaker，换人档恒早于同人档", () => {
  const h = hub();
  const script: Array<[number, number]> = [
    [0, A],
    [300, B],
    [600, A],
    [900, B],
    [1200, A],
    [1500, B],
  ];
  const tiers: Array<string | null> = [];
  const deadlines: Array<number | null> = [];
  for (const [now, senderId] of script) {
    h.observe(msg(senderId, `${now}ms 的话`), now);
    tiers.push(h.state(K).tier);
    deadlines.push(h.state(K).deadline);
  }

  assert.deepEqual(tiers, [
    "quietOpen",
    "switchSpeaker",
    "switchSpeaker",
    "switchSpeaker",
    "switchSpeaker",
    "switchSpeaker",
  ]);
  assert.deepEqual(deadlines, [2000, 800, 1100, 1400, 1700, 2000]);
  // 每一次换人的 deadline 都正好是 now + 500，并且严格早于同人档 now + 2000
  for (let index = 1; index < script.length; index += 1) {
    const now = script[index]![0];
    assert.equal(deadlines[index], now + 500, `第 ${index + 1} 条应为 now+switchSpeaker`);
    assert.ok((deadlines[index] as number) < now + 2000, `第 ${index + 1} 条的换人档必须早于同人档`);
  }

  const batch = h.consume(K, 2000);
  assert.ok(batch);
  assert.equal(batch.msgs.length, 6);
  assert.deepEqual(
    batch.msgs.map((m) => m.id),
    [A, B, A, B, A, B],
  );
  assert.equal(batch.waited_ms, 1500);
});

test("②b A、B 交替 6 条、间隔 3s、硬顶 8s：0/3/6 一段、9/12/15 一段，段间不串人", () => {
  const h = hub();
  for (const [now, senderId] of [
    [0, A],
    [3000, B],
    [6000, A],
  ] as Array<[number, number]>) {
    h.observe(msg(senderId, `${now}ms 的话`), now);
  }
  // ⚠ 本条假设"取件时刻落在硬顶 8s"：第 1 条 deadline 冻在 2000，3s 与 6s 两条都是到期后并入，
  // 于是才凑出 0/3/6 一段。实际接线是"到期即出手"（按 hub 报的 deadline 定时 consume），8s 只是上限不是目标，
  // 届时同一串消息会切成 4 段 [1,2,2,1]（见 server 侧 drill G3）。
  // 所以下面的分段数字只能用来说明"到期并入 + 硬顶封顶"，不许当成分段依据。
  assert.equal(h.state(K).deadline, 2000);
  const first = h.consume(K, 8000);
  assert.ok(first);
  assert.equal(first.batchId, "b1");
  assert.equal(first.firstAt, 0);
  assert.deepEqual(
    first.msgs.map((m) => m.id),
    [A, B, A],
  );
  assert.deepEqual(
    first.msgs.map((m) => m.at),
    [0, 3000, 6000],
  );

  for (const [now, senderId] of [
    [9000, B],
    [12000, A],
    [15000, B],
  ] as Array<[number, number]>) {
    h.observe(msg(senderId, `${now}ms 的话`), now);
  }
  const second = h.consume(K, 16000);
  assert.ok(second);
  assert.equal(second.batchId, "b2");
  assert.equal(second.firstAt, 9000);
  assert.deepEqual(
    second.msgs.map((m) => m.id),
    [B, A, B],
  );
  assert.deepEqual(
    second.msgs.map((m) => m.at),
    [9000, 12000, 15000],
  );
  assert.equal(h.consume(K, 16000), null);
});

test("③ 第 7 条在 9s 后到：第一段在 firstAt+8000 出手，第 7 条落进第二段", () => {
  const h = hub();
  for (let index = 0; index < 6; index += 1) h.observe(msg(A, `第${index + 1}句`), index * 1500);

  assert.equal(h.state(K).deadline, 8000); // 同人续发一路顶到硬顶 firstAt+8000
  assert.equal(h.consume(K, 7999), null); // 差 1ms 都不出手

  const first = h.consume(K, 8000);
  assert.ok(first);
  assert.equal(first.batchId, "b1");
  assert.equal(first.firstAt, 0);
  assert.equal(first.lastAt, 7500);
  assert.equal(first.msgs.length, 6);

  h.observe(msg(B, "九秒后的一句话"), 9000);
  assert.equal(h.state(K).firstAt, 9000);
  assert.equal(h.state(K).tier, "activeOpen"); // 距上一条 1500ms，不算安静
  assert.equal(h.state(K).deadline, 14000);
  assert.equal(h.consume(K, 9000), null);
  const second = h.consume(K, 14000);
  assert.ok(second);
  assert.equal(second.batchId, "b2");
  assert.equal(second.firstAt, 9000);
  assert.equal(second.msgs.length, 1);
  assert.equal(second.msgs[0].text, "九秒后的一句话");
});

test("边界：now == deadline 仍可续（deadline 变大）；now == deadline+1 只并入、deadline 不动", () => {
  const alive = hub();
  alive.observe(msg(A, "第 1 句"), 0);
  assert.equal(alive.state(K).deadline, 2000);
  alive.observe(msg(A, "正好 2000 来的第 2 句"), 2000);
  assert.equal(alive.state(K).tier, "sameSpeaker");
  assert.equal(alive.state(K).deadline, 4000); // 续上去了

  const late = hub();
  late.observe(msg(A, "第 1 句"), 0);
  late.observe(msg(A, "2001 才来的第 2 句"), 2001);
  assert.equal(late.state(K).deadline, 2000); // 只并入，一个字节都不延长
  assert.equal(late.state(K).count, 2);
  assert.equal(late.state(K).tier, "quietOpen"); // tier 描述上一次 deadline 怎么算的，跟着冻住

  const batch = late.consume(K, 2001);
  assert.ok(batch);
  assert.equal(batch.msgs.length, 2);
});

test("边界：now == firstAt+hardCap 时再来一条，deadline 仍是 8000（出手不推后）", () => {
  const h = hub();
  for (let index = 0; index < 6; index += 1) h.observe(msg(A, `第${index + 1}句`), index * 1500);
  assert.equal(h.state(K).deadline, 8000);

  h.observe(msg(B, "第 8 秒整来的这一条"), 8000);
  assert.equal(h.state(K).count, 7);
  assert.equal(h.state(K).deadline, 8000); // min(8000 + 2000, 0 + 8000) = 8000

  const batch = h.consume(K, 8000);
  assert.ok(batch);
  assert.equal(batch.msgs.length, 7);
  assert.equal(batch.lastAt, 8000);
});

test("被 @：开批即封到 firstAt+1000，后续补 @ 只取最早的那一刻", () => {
  const h = hub();
  h.observe(msg(A, "叫她一声", { mentioned: true }), 0);
  assert.equal(h.state(K).atFirst, 0);
  assert.equal(h.state(K).deadline, 1000); // min(quietOpen 2000, @ 1000)
  assert.ok((h.state(K).deadline ?? Infinity) <= 0 + 1000);

  // 已开批里补一个 @：同人续发的 3000 被 "@ 那一刻 +1s" 压到 2000
  const late = hub();
  late.observe(msg(A, "一"), 0); // quietOpen → 2000
  late.observe(msg(A, "二", { mentioned: true }), 1000); // sameSpeaker 1000+2000=3000 → @ 上限 2000
  assert.equal(late.state(K).atFirst, 1000);
  assert.equal(late.state(K).deadline, 2000);
  // 换人那一刻再补一句 @：上限仍然绑在最早那次（1000+1000），不会被最后一条改写成 1500+1000
  late.observe(msg(B, "三", { mentioned: true }), 1500);
  assert.equal(late.state(K).atFirst, 1000);
  assert.equal(late.state(K).deadline, 2000);
});

test("裸图：开批至少等 4s，但抬不过硬顶，也不会把更长的 tier 压短", () => {
  const h = hub();
  h.observe(msg(A, "[图片]", { bareImage: true }), 0);
  assert.equal(h.state(K).deadline, 4000); // quietOpen 2000 被裸图抬到 4000
  assert.ok((h.state(K).deadline ?? 0) >= 0 + 4000);

  // 活跃批 1500 < 4000：裸图把这一批抬到 now+4000
  const lift = hub();
  lift.observe(msg(A, "在吗"), 0);
  lift.observe(msg(B, "[图片]", { bareImage: true }), 1000);
  assert.equal(lift.state(K).deadline, 5000);

  // 一路活到 5000 再发图：now+4000=9000 仍被 firstAt+8000 压回去
  const capped = hub();
  for (let index = 0; index < 5; index += 1) capped.observe(msg(A, `第${index + 1}句`), index * 1000);
  capped.observe(msg(B, "[图片]", { bareImage: true }), 5000);
  assert.equal(capped.state(K).deadline, 8000);
});

test("纯表情：只能提前不能延后", () => {
  const h = hub();
  h.observe(msg(A, "一"), 0); // 2000
  h.observe(msg(A, "[表情:14]", { sticker: true }), 500); // sameSpeaker 2500 → 表情压到 1000
  assert.equal(h.state(K).deadline, 1000);
  assert.ok(1000 < 2500);

  const fresh = hub();
  fresh.observe(msg(A, "[表情:1]", { sticker: true }), 0);
  assert.equal(fresh.state(K).deadline, 500); // 开批就是表情：500ms 就出手，不被 2s 拖住
});

test("私聊：每条即批、0 延迟，privateCapMs 从不触发上限", () => {
  const P: Where = { scene: "private", userId: 3000000003 };
  const h = hub();
  h.observe(msg(3000000003, "第一条", { where: P }), 1000);
  assert.equal(h.state(PK).deadline, 1000); // 0 延迟
  h.observe(msg(3000000003, "第二条", { where: P }), 1010);

  const first = h.consume(PK, 1010);
  const second = h.consume(PK, 1010);
  assert.ok(first && second);
  assert.deepEqual([first.batchId, second.batchId], ["b1", "b2"]);
  assert.equal(first.msgs.length, 1);
  assert.equal(first.msgs[0].text, "第一条");
  assert.equal(first.firstAt, 1000);
  assert.equal(first.waited_ms, 0);
  assert.equal(first.scene, "private");
  assert.equal(first.userId, 3000000003);
  assert.equal("groupId" in first, false);
  assert.equal(second.msgs[0].text, "第二条");
  assert.equal(second.firstAt, 1010);
  assert.equal(h.consume(PK, 1010), null);

  // 系数被改成 0 也只会得到 min(now, now+0) = now：私聊永远不可能是"等一会儿再回"。
  const zeroCap = hub({ privateCapMs: 0 });
  zeroCap.observe(msg(3000000003, "私聊", { where: P }), 5000);
  assert.equal(zeroCap.state(PK).deadline, 5000);
});

test("系数活读：hardCap / sameSpeaker / quietGap 都由注入的 policy 说话", () => {
  const h = hub({ hardCapMs: 1234, tiersMs: { quietOpen: 2000, activeOpen: 5000, sameSpeaker: 777, switchSpeaker: 500 } });
  h.observe(msg(A, "一"), 0);
  assert.equal(h.state(K).deadline, 1234); // quietOpen 2000 被注入了 1234 的硬顶压住
  h.observe(msg(A, "二"), 100); // 100 + 777
  assert.equal(h.state(K).deadline, 877);
  h.observe(msg(A, "三"), 700); // 700 + 777 = 1477 → 硬顶 1234
  assert.equal(h.state(K).deadline, 1234);

  const tight = hub({ quietGapMs: 1000 });
  tight.observe(msg(A, "一"), 0);
  tight.consume(K, 2000);
  tight.observe(msg(A, "二"), 2000); // 隔了 2000 > 1000，算安静
  assert.equal(tight.state(K).tier, "quietOpen");
  assert.equal(tight.state(K).deadline, 4000);

  const loose = hub({ quietGapMs: 5000 });
  loose.observe(msg(A, "一"), 0);
  loose.consume(K, 2000);
  loose.observe(msg(A, "二"), 2000); // 隔了 2000 < 5000，群里还算热闹
  assert.equal(loose.state(K).tier, "activeOpen");
  assert.equal(loose.state(K).deadline, 7000);
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
  assert.deepEqual(atMe.atTargets, ["1000000001"]);
  assert.equal(atMe.isBareImage, false);
  assert.equal(atMe.isSticker, false);
  assert.equal(atMe.hasTextSegment, true);

  const atOther = expectParsed(groupEvent([atSeg("3000000002"), textSeg("你来")]));
  assert.equal(atOther.mentionedSelf, false);
  assert.deepEqual(atOther.atTargets, ["3000000002"]);

  const atAll = expectParsed(groupEvent([atSeg("all"), textSeg("开会了")]));
  assert.equal(atAll.mentionedAll, true);
  assert.equal(atAll.mentionedSelf, false);

  const bareImage = expectParsed(groupEvent([{ type: "image", data: { file: "ab12cd34.image" } }]));
  assert.equal(bareImage.isBareImage, true);
  assert.equal(bareImage.hasTextSegment, false);
  assert.equal(bareImage.hasImage, true);

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
  assert.equal(mentionedHub.state(K).deadline, 1000);

  const imageHub = hub();
  imageHub.observe(fromEvent(bareImage), 0);
  assert.equal(imageHub.state(K).deadline, 4000);

  const stickerHub = hub();
  stickerHub.observe(fromEvent(sticker), 0);
  assert.equal(stickerHub.state(K).deadline, 500);
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

  // 两个会话各自一个 open，谁也别替谁排队
  assert.deepEqual(h.openKeys().sort(), [KA, KB].sort());

  const a = h.consume(KA, 4000);
  const b = h.consume(KB, 4000);
  assert.ok(a && b);
  assert.equal(a.msgs.length, 2);
  assert.equal(b.msgs.length, 2);
  assert.deepEqual(a.msgs.map((m) => m.id), [A, A]);
  assert.deepEqual(b.msgs.map((m) => m.id), [B, B]);
  assert.deepEqual(a.msgs.map((m) => m.text), ["A 群 0", "A 群 600"]);
  assert.deepEqual(b.msgs.map((m) => m.text), ["B 群 300", "B 群 900"]);
  assert.equal(a.scene, "group");
  assert.equal(a.groupId, 811111111);
  assert.equal(b.groupId, 822222222);
  assert.equal(a.firstAt, 0);
  assert.equal(b.firstAt, 300);
  // 取走之后各自清空，互不影响
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

  // B 群 1s / 2s / 3s 连说三句（每段都取走），把 B 的「上一条到达」推到 3s——但只属于 B
  h.observe({ ...msg(B, "B 群 1s"), where: GB }, 1000);
  h.consume(KB, 3000);
  h.observe({ ...msg(B, "B 群 2s"), where: GB }, 2000);
  h.consume(KB, 7000);
  h.observe({ ...msg(B, "B 群 3s"), where: GB }, 3000);
  h.consume(KB, 8000);

  // A 群 31 秒后才说话：A 自己的上一条是 0ms → 31s > quietGapMs(30s) → 安静档。
  // 若 lastArrivalAt 还是进程级单值（3s），这里会算成 28s → activeOpen，所以这条是真的分离判据。
  h.observe({ ...msg(A, "A 群 31 秒后"), where: GA }, 31000);
  assert.equal(h.state(KA).tier, "quietOpen");
  assert.equal(h.state(KA).deadline, 33000); // 31s + quietOpen 2000

  // 对照：B 群自己上一句是 3s，31s 只隔 28s ≤ 30s → 活跃档
  h.observe({ ...msg(B, "B 群 31 秒"), where: GB }, 31000);
  assert.equal(h.state(KB).tier, "activeOpen");
  assert.equal(h.state(KB).deadline, 36000); // 31s + activeOpen 5000
});
