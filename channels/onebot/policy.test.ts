import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_POLICY_PATH, TIERS, loadPolicy, readPolicy } from "./policy.ts";

test("落盘系数：合批/出站/配额/决策四段的每个数字都对得上冻结值", () => {
  const policy = loadPolicy();

  assert.equal(policy.batch.quietGapMs, 30000);
  assert.deepEqual(policy.batch.openMs, { quietOpen: 2000, activeOpen: 5000, privateOpen: 3500 });
  assert.deepEqual(policy.batch.continueMs, { group: 3000, private: 3500 });
  assert.equal(policy.batch.maxMsgs, 6);
  assert.equal(policy.batch.hardCapMs, 8000);
  assert.equal(policy.batch.mentionCapMs, 1000);
  assert.equal(policy.batch.bareImageMs, 4000);
  assert.equal(policy.batch.stickerMs, 500);

  assert.equal(policy.outbound.maxChars, 512);
  assert.equal(policy.outbound.maxSentences, 8);
  assert.equal(policy.outbound.maxChunks, 3);
  assert.equal(policy.outbound.defaultReplies.length, 3);
  assert.deepEqual(policy.outbound.defaultReplies, [
    "嗯，我在。",
    "……我在听。",
    "这句我想说的太长，先简短回一句：我在。",
  ]);

  assert.equal(policy.quota.windowMs, 3600000);
  assert.equal(policy.quota.perGroupMax, 6);
  assert.equal(policy.quota.mentionCanExceed, true);

  assert.deepEqual(policy.decide.reasonCodes, [
    "sent",
    "silent_mode",
    "quota_exceeded",
    "core_error",
    "outbound_failed",
    "core_empty",
    "replaced_too_long",
    "no_mention",
  ]);
});

test("默认路径按模块位置解析到 data/policy.json，不是按 cwd", () => {
  assert.ok(DEFAULT_POLICY_PATH.endsWith("/channels/onebot/data/policy.json"), DEFAULT_POLICY_PATH);
  assert.deepEqual([...TIERS], ["quietOpen", "activeOpen", "privateOpen", "continuing"]);
});

test("读盘失败：文件不存在说清路径，不静默回落成内置默认值", () => {
  assert.throws(() => loadPolicy("/nonexistent/policy.json"), /读不到系数文件 \/nonexistent\/policy\.json/);
});

test("形状校验：缺键、类型错、负数、非整数都在读盘这一刻炸掉", () => {
  const good = () => structuredClone(loadPolicy()) as unknown as Record<string, any>;

  assert.throws(() => readPolicy(null), /应是 JSON 对象/);

  const noBatch = good();
  delete noBatch.batch;
  assert.throws(() => readPolicy(noBatch), /缺少 batch 段/);

  const noPrivateOpen = good();
  delete noPrivateOpen.batch.openMs.privateOpen;
  assert.throws(() => readPolicy(noPrivateOpen), /batch\.openMs\.privateOpen 应为有限数字，实际是（缺失）/);

  const negative = good();
  negative.batch.quietGapMs = -1;
  assert.throws(() => readPolicy(negative), /batch\.quietGapMs 不能是负数/);

  const zeroChars = good();
  zeroChars.outbound.maxChars = 0;
  assert.throws(() => readPolicy(zeroChars), /outbound\.maxChars 应为 >=1 的整数/);

  const floatChunks = good();
  floatChunks.outbound.maxChunks = 2.5;
  assert.throws(() => readPolicy(floatChunks), /outbound\.maxChunks 应为 >=1 的整数/);

  const emptyReplies = good();
  emptyReplies.outbound.defaultReplies = [];
  assert.throws(() => readPolicy(emptyReplies), /outbound\.defaultReplies 应为非空数组/);

  const badFlag = good();
  badFlag.quota.mentionCanExceed = "yes";
  assert.throws(() => readPolicy(badFlag), /quota\.mentionCanExceed 应为布尔/);

  const blankCode = good();
  blankCode.decide.reasonCodes = ["sent", "  "];
  assert.throws(() => readPolicy(blankCode), /decide\.reasonCodes\[1\] 应为非空字符串/);
});

test("未注入的系数不会被悄悄补上：readPolicy 只认文件里真有的键", () => {
  const policy = readPolicy(JSON.parse(JSON.stringify(loadPolicy())));
  assert.deepEqual(policy, loadPolicy());
  // 注入即生效：改过的副本不该影响落盘的那份
  const injected = readPolicy(JSON.parse(JSON.stringify(loadPolicy())));
  injected.batch.hardCapMs = 100;
  assert.equal(injected.batch.hardCapMs, 100);
  assert.equal(loadPolicy().batch.hardCapMs, 8000);
});
