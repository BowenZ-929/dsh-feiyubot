import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { countChars, countSentences, enforceLimits, pickDefaultReply } from "./compose.ts";
import type { Chunk } from "./outbox.ts";
import type { ComposePolicy } from "./compose.ts";

// 系数全部从 policy 注入，测试里用的是 data/policy.json 冻结的那份值。
const policy: ComposePolicy = {
  outbound: {
    maxChars: 512,
    maxSentences: 8,
    maxChunks: 3,
    defaultReplies: ["嗯，我在。", "……我在听。", "这句我想说的太长，先简短回一句：我在。"],
  },
};

const chunk = (text: string): Chunk => ({ text, gap_before: 0, send_at: 0 });

test("上限：界内原样返回，reason 为 null（连数组都是同一份）", () => {
  const chunks = [chunk("嗯。"), chunk("我在听")];
  const result = enforceLimits(chunks, "b1", policy);
  assert.equal(result.reason, null);
  assert.deepEqual(result.chunks, chunks);
});

test("上限：字数 512 不越界，513 越界", () => {
  const atLimit = "字".repeat(512);
  assert.equal(countChars(atLimit), 512);
  assert.equal(enforceLimits([chunk(atLimit)], "b2", policy).reason, null);
  const over = "字".repeat(513);
  assert.equal(enforceLimits([chunk(over)], "b3", policy).reason, "replaced_too_long");
});

test("上限：句子按 。！？!?…；; 与换行切，末尾无标点也算一句", () => {
  assert.equal(countSentences("你好。今天天气不错"), 2);
  assert.equal(countSentences("走吗？走！走…走；走;走\n走"), 7);
  assert.equal(countSentences("   "), 0);
  assert.equal(countSentences(""), 0);
  const eight = "好。".repeat(8);
  assert.equal(countSentences(eight), 8);
  assert.equal(enforceLimits([chunk(eight)], "b4", policy).reason, null);
  assert.equal(enforceLimits([chunk("好。".repeat(9))], "b5", policy).reason, "replaced_too_long");
});

test("上限：条数 3 不越界，4 越界", () => {
  assert.equal(enforceLimits([chunk("一"), chunk("二"), chunk("三")], "b6", policy).reason, null);
  assert.equal(
    enforceLimits([chunk("一"), chunk("二"), chunk("三"), chunk("四")], "b7", policy).reason,
    "replaced_too_long",
  );
});

test("上限：越界整批换成恰好一条默认短句，绝不截断半句", () => {
  const long = "这句话特别长。".repeat(20);
  const result = enforceLimits([chunk(long), chunk("第二条")], "b8", policy);
  assert.equal(result.reason, "replaced_too_long");
  assert.equal(result.chunks.length, 1);
  assert.equal(result.chunks[0].send_at, 0);
  assert.equal(result.chunks[0].gap_before, 0);
  assert.ok(policy.outbound.defaultReplies.includes(result.chunks[0].text), "替换句必须来自 policy 里的备选");
  assert.equal(long.startsWith(result.chunks[0].text), false, "不许是原文前缀");
  assert.equal(long.includes(result.chunks[0].text), false, "不许是原文的任意片段");
  assert.ok(result.chunks[0].text.length < 40, "替换句必须是短的");
});

test("上限：同一 batchId 两次调用选出同一句（sha256 前 8 位 hex 取模，可复现）", () => {
  const first = enforceLimits([chunk("字".repeat(600))], "b42", policy).chunks[0].text;
  const second = enforceLimits([chunk("句。".repeat(9))], "b42", policy).chunks[0].text;
  const third = enforceLimits([chunk("一"), chunk("二"), chunk("三"), chunk("四")], "b42", policy).chunks[0].text;
  assert.equal(first, second);
  assert.equal(second, third);

  const expectedIndex =
    parseInt(createHash("sha256").update("b42", "utf8").digest("hex").slice(0, 8), 16) % 3;
  assert.equal(first, policy.outbound.defaultReplies[expectedIndex], "选句公式必须与契约一致");
  assert.equal(pickDefaultReply("b42", policy.outbound.defaultReplies), first);
});

test("上限：不同 batchId 会选到不同的句子（不是永远第一句）", () => {
  const seen = new Set<string>();
  for (let i = 0; i < 40; i += 1) {
    seen.add(pickDefaultReply(`b${i}`, policy.outbound.defaultReplies));
  }
  assert.equal(seen.size, policy.outbound.defaultReplies.length);
});

test("上限：空 chunks 落 core_empty，reason 不是 replaced_too_long", () => {
  const result = enforceLimits([], "b9", policy);
  assert.deepEqual(result.chunks, []);
  assert.equal(result.reason, "core_empty");
});

test("上限：policy 没备默认短句时不现编，落 core_empty", () => {
  const bare: ComposePolicy = { outbound: { ...policy.outbound, defaultReplies: [] } };
  const result = enforceLimits([chunk("字".repeat(600))], "b10", bare);
  assert.deepEqual(result.chunks, []);
  assert.equal(result.reason, "core_empty");
});

test("上限：只越界其中一条也整批换掉（第二条超字数）", () => {
  const chunks = [chunk("短。"), chunk("字".repeat(513)), chunk("也短。")];
  const result = enforceLimits(chunks, "b11", policy);
  assert.equal(result.chunks.length, 1);
  assert.equal(result.reason, "replaced_too_long");
  assert.equal(result.chunks[0].text.includes("短。"), false);
});
