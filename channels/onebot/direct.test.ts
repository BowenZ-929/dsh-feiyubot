import test from "node:test";
import assert from "node:assert/strict";
import { firstLayer, loadGateWords } from "./direct.ts";

test("本 bot 的语音口令可进入对话，其他命令仍拦下", () => {
  const input = { scene: "group" as const, mentionedSelf: false, quotedHer: false, isSticker: false, isBareImage: false };
  const words = loadGateWords();
  assert.deepEqual(firstLayer({ ...input, text: "/语音 晚安" }, words), { kind: "directed", rule: "voice" });
  for (const text of ["/help", "/语音管理", "／未知命令"]) {
    assert.deepEqual(firstLayer({ ...input, text }, words), { kind: "blocked", reason: "command" });
  }
});

test("活动对话补充作为定向，普通闲聊及命令边界保持", () => {
  const input = { scene: "group" as const, mentionedSelf: false, quotedHer: false, isSticker: false, isBareImage: false,
    continuation: true, text: "不是明天，是后天" };
  const words = loadGateWords();
  assert.deepEqual(firstLayer(input, words), { kind: "directed", rule: "continuation" });
  assert.deepEqual(firstLayer({ ...input, continuation: false }, words), { kind: "chitchat" });
  assert.deepEqual(firstLayer({ ...input, text: "/help" }, words), { kind: "blocked", reason: "command" });
  assert.deepEqual(firstLayer({ ...input, mentionedSelf: true }, words), { kind: "directed", rule: "at" });
});
