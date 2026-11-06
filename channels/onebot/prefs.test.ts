import { test } from "node:test";
import assert from "node:assert/strict";
import { capFor, foldPrefs, foldPrefsStats } from "./prefs.ts";

const jsonl = (...rows: unknown[]): string => rows.map((row) => JSON.stringify(row)).join("\n") + "\n";

test("按人折最新：同一个 by / who 取最后一行（家的卡用 by）", () => {
  const text = jsonl(
    { by: "1000aaaa", params: { chunks_per_reply: 1 } },
    { by: "1000aaaa", params: { chunks_per_reply: 2 } },
    { who: 222, params: { chunks_per_reply: 3 } },
    { name: "333", params: { chunks_per_reply: 1 } },
  );
  const prefs = foldPrefs(text);
  assert.equal(prefs.get("1000aaaa")?.chunksPerReply, 2);
  assert.equal(prefs.get("222")?.chunksPerReply, 3);
  assert.equal(prefs.get("333")?.chunksPerReply, 1);
});

test("坏行/半行跳过并计数，不让整份失效", () => {
  const text = '{"by":"1000aaaa","params":{"chunks_per_reply":2}}\n{"by":"222",\n[1,2]\n';
  const result = foldPrefsStats(text);
  assert.equal(result.prefs.get("1000aaaa")?.chunksPerReply, 2);
  assert.equal(result.skipped, 2);
});

test("值只认 1–3 的整数：9 / 0 / \"2\" / 1.5 一律当没有这个键（并计数为非法值）", () => {
  for (const bad of [9, 0, "2", 1.5, -1]) {
    const result = foldPrefsStats(jsonl({ by: "1000aaaa", params: { chunks_per_reply: bad } }));
    assert.equal(result.prefs.has("1000aaaa"), false, JSON.stringify(bad));
    assert.equal(result.invalid, 1, JSON.stringify(bad));
    assert.equal(capFor("1000aaaa", result.prefs, 3), 3, JSON.stringify(bad));
  }
});

test("缺卡 / 没这个键 / who 为空 → 回落 fallback", () => {
  const prefs = foldPrefs(jsonl({ by: "1000aaaa", params: {} }, { by: "222" }));
  assert.equal(capFor("1000aaaa", prefs, 3), 3);
  assert.equal(capFor("222", prefs, 3), 3);
  assert.equal(capFor("999", prefs, 3), 3);
  assert.equal(capFor(null, prefs, 3), 3);
});

test("有卡就按卡来（1–3 都认），数字 id 与字符串 id 等价", () => {
  const prefs = foldPrefs(jsonl({ by: "3100000001", params: { chunks_per_reply: 1 } }));
  assert.equal(capFor("3100000001", prefs, 3), 1);
  assert.equal(capFor(3100000001, prefs, 3), 1);
});
