import { test } from "node:test";
import assert from "node:assert/strict";
import { BatchHub } from "./batch.ts";
import type { BatchSnapshot, Where } from "./batch.ts";
import { renderSegment } from "./segment.ts";
import { loadPolicy } from "./policy.ts";

const A = 3000000001;
const B = 3000000002;
const C = 3000000004;
const GROUP: Where = { scene: "group", groupId: 700000001 };
// 会话键与 gate.targetOf 同源（M4 起 hub 按会话分批：state/consume 都要 key）
const K = "group:700000001";
const PK = "user:3000000003";

const hub = () => new BatchHub(structuredClone(loadPolicy()).batch);

test("段渲染：3 条（含 @ 她 + 一条带换行）→ 每行 昵称(id)：原话，换行原样保留", () => {
  const h = hub();
  h.observe(
    { senderId: A, senderName: "小明", text: "在吗", mentioned: false, bareImage: false, sticker: false, where: GROUP },
    0,
  );
  h.observe(
    {
      senderId: B,
      senderName: "小美",
      text: "在的\n刚吃完饭",
      mentioned: true,
      bareImage: false,
      sticker: false,
      where: GROUP,
    },
    300,
  );
  h.observe(
    { senderId: C, senderName: "阿明", text: "那我说了", mentioned: false, bareImage: false, sticker: false, where: GROUP },
    600,
  );

  const batch = h.consume(K, 3600);
  assert.ok(batch);
  const rendered = renderSegment(batch);

  assert.equal(
    rendered.text,
    "小明(3000000001)：在吗\n小美(3000000002)：在的\n小美(3000000002)：刚吃完饭\n阿明(3000000004)：那我说了",
  );
  // 3 条消息渲染出 4 个物理行：带换行的那条摊成两行，且续行也带着她的人名。
  assert.equal(rendered.text.split("\n").length, 4);
  assert.equal(rendered.text.split("\n")[2], "小美(3000000002)：刚吃完饭");

  assert.deepEqual(rendered.batch, {
    id: "b1",
    count: 3,
    waited_ms: 600,
    tier: "continuing",
    mentioned: true,
    at: "1970-01-01T00:00:00.000Z",
    scene: "group",
    groupId: 700000001,
    msgs: [
      { id: A, name: "小明", text: "在吗", at: "1970-01-01T00:00:00.000Z", mentioned: false },
      { id: B, name: "小美", text: "在的\n刚吃完饭", at: "1970-01-01T00:00:00.300Z", mentioned: true },
      { id: C, name: "阿明", text: "那我说了", at: "1970-01-01T00:00:00.600Z", mentioned: false },
    ],
  });
});

test("段渲染：每一条物理行都带 speaker——两行消息 + 单行消息 = 3 行，结构化仍是 2 条", () => {
  const h = hub();
  h.observe(
    {
      senderId: B,
      senderName: "小美",
      text: "在的\n刚吃完饭",
      mentioned: false,
      bareImage: false,
      sticker: false,
      where: GROUP,
    },
    0,
  );
  h.observe(
    { senderId: A, senderName: "小明", text: "那我去洗碗", mentioned: false, bareImage: false, sticker: false, where: GROUP },
    100,
  );

  const batch = h.consume(K, 3100);
  assert.ok(batch);
  const rendered = renderSegment(batch);
  const lines = rendered.text.split("\n");

  assert.equal(lines.length, 3);
  for (const line of lines) assert.match(line, /^.+\(\d+\)：/);
  assert.deepEqual(lines, ["小美(3000000002)：在的", "小美(3000000002)：刚吃完饭", "小明(3000000001)：那我去洗碗"]);

  // 结构化那一半不受影响：还是两条，原话里的换行一个字都没动。
  assert.equal(rendered.batch.msgs.length, 2);
  assert.equal(rendered.batch.msgs[0]?.text, "在的\n刚吃完饭");
  assert.ok(rendered.batch.msgs[0]?.text.includes("\n") === true);
});

test("段渲染：空行照留，连续两个换行不被压成一个", () => {
  const snapshot: BatchSnapshot = {
    batchId: "b7",
    msgs: [{ id: 3000000005, name: "小美", text: "上一句\n\n下一段", at: 0, mentioned: false }],
    tier: "quietOpen",
    firstAt: 0,
    lastAt: 0,
    waited_ms: 0,
    mentioned: false,
    scene: "group",
    groupId: 700000001,
  };
  const lines = renderSegment(snapshot).text.split("\n");
  assert.deepEqual(lines, ["小美(3000000005)：上一句", "小美(3000000005)：", "小美(3000000005)：下一段"]);
});

test("段渲染：私聊只有 userId，没有 groupId", () => {
  const P: Where = { scene: "private", userId: 3000000003 };
  const h = hub();
  h.observe(
    { senderId: 3000000003, senderName: "路人甲", text: "在吗", mentioned: false, bareImage: false, sticker: false, where: P },
    1000,
  );
  const batch = h.consume(PK, 4500);
  assert.ok(batch);
  const rendered = renderSegment(batch);

  assert.equal(rendered.batch.scene, "private");
  assert.equal(rendered.batch.userId, 3000000003);
  assert.equal("groupId" in rendered.batch, false);
  assert.equal(rendered.text, "路人甲(3000000003)：在吗");
});

test("段渲染：昵称缺省时退回 QQ，绝不出现空人名", () => {
  const snapshot: BatchSnapshot = {
    batchId: "b9",
    msgs: [{ id: 3000000009, name: "   ", text: "嗯", at: 0, mentioned: false }],
    tier: "quietOpen",
    firstAt: 0,
    lastAt: 0,
    waited_ms: 0,
    mentioned: false,
    scene: "group",
    groupId: 700000001,
  };
  const rendered = renderSegment(snapshot);
  assert.equal(rendered.text, "3000000009(3000000009)：嗯");
  assert.equal(rendered.batch.msgs[0]?.name, "3000000009");
});
