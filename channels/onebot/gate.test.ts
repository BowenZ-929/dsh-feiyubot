import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  LEDGER_RELATIVE_PATH,
  Ledger,
  MODE_ENV,
  dedupKey,
  dedupNote,
  makeRow,
  needsErrorRow,
  readMode,
  sceneOf,
  targetOf,
} from "./gate.ts";

// 每个用例一个独立 tmp 目录：账是追加写的，共用目录会让用例互相污染。
const freshLedgerPath = (): string => {
  const dir = mkdtempSync(join(tmpdir(), "onebot-gate-"));
  return join(dir, LEDGER_RELATIVE_PATH);
};

test("模式：不给环境变量 = silent", () => {
  const decision = readMode({});
  assert.equal(decision.mode, "silent");
  assert.equal(decision.warn, null);
  assert.equal(readMode({ [MODE_ENV]: "silent" }).mode, "silent");
  assert.equal(readMode({ [MODE_ENV]: "send" }).mode, "send");
});

test("模式：认不出的值一律落 silent，并留下原值", () => {
  for (const raw of ["", "SEND", "Send", "sen", "sending", "1", "true", " silent"]) {
    const decision = readMode({ [MODE_ENV]: raw });
    assert.equal(decision.mode, "silent", `原值 ${JSON.stringify(raw)} 不该被当成 send`);
    assert.ok(decision.warn !== null, `原值 ${JSON.stringify(raw)} 必须留一行 warn`);
    assert.match(decision.warn, /QQAGENT_CHANNEL_MODE= 认不出/);
    assert.ok(decision.warn.includes(JSON.stringify(raw)), "warn 里要能看见原始值");
  }
});

test("目标：群/私聊分别落到 group: / user:", () => {
  assert.equal(targetOf({ scene: "group", groupId: 7 }), "group:7");
  assert.equal(targetOf({ scene: "private", userId: 9 }), "user:9");
});

test("去重键：有 messageId 就用 mid，没有就用内容指纹", () => {
  const base = { scene: "group" as const, target: "group:7", whoId: 3, time: 1761523200, text: "在吗" };
  assert.equal(dedupKey({ ...base, messageId: 2000000001 }), "mid:2000000001");

  const fingerprint = dedupKey({ ...base, messageId: null });
  assert.match(fingerprint, /^fp:[0-9a-f]{16}$/);
  assert.equal(dedupKey({ ...base, messageId: null }), fingerprint, "同样输入必须同样指纹");
  assert.notEqual(dedupKey({ ...base, messageId: null, text: "不在" }), fingerprint);
  assert.notEqual(dedupKey({ ...base, messageId: null, time: 1761523201 }), fingerprint);
});

test("账行：字段固定、顺序固定、缺省一律 null", () => {
  const row = makeRow(
    { kind: "blocked", mode: "silent", key: "mid:1", messageId: 1, scene: "group", target: "group:7" },
    "2026-01-01T00:00:00.000Z",
  );
  assert.deepEqual(Object.keys(row), [
    "ts",
    "kind",
    "mode",
    "key",
    "messageId",
    "scene",
    "target",
    "index",
    "text",
    "send_at",
    "retcode",
    "status",
    "note",
  ]);
  assert.equal(row.index, null);
  assert.equal(row.text, null);
  assert.equal(row.send_at, null);
  assert.equal(row.retcode, null);
  assert.equal(row.status, null);
  assert.equal(row.note, "");
});

test("账：追加写会自动建 .state/，同一份账新开实例仍认得出重复（模拟重启）", () => {
  const path = freshLedgerPath();
  const warns: string[] = [];
  const clock = () => new Date("2026-01-01T00:00:00.000Z");

  const first = new Ledger({ path, now: clock, onWarn: (message) => warns.push(message) });
  assert.equal(first.firstKind("mid:1"), null);
  first.appendNew({ kind: "blocked", mode: "silent", key: "mid:1", messageId: 1, scene: "group", target: "group:7", text: "想说的话" });

  assert.equal(existsSync(path), true, "账文件必须被建出来");
  const lines = readFileSync(path, "utf8").trim().split("\n");
  assert.equal(lines.length, 1);
  const written = JSON.parse(lines[0]);
  assert.equal(written.kind, "blocked");
  assert.equal(written.text, "想说的话");
  assert.equal(written.ts, "2026-01-01T00:00:00.000Z");

  // 同一个文件换一个实例 = 新进程：去重集合必须从盘上重建。
  const second = new Ledger({ path, now: clock, onWarn: (message) => warns.push(message) });
  assert.equal(second.firstKind("mid:1"), "blocked");
  assert.deepEqual(warns, []);
});

test("账：坏行不能让整本账失效", () => {
  const path = freshLedgerPath();
  const first = new Ledger({ path });
  first.appendNew({ kind: "sent", mode: "send", key: "mid:2", messageId: 2, scene: "private", target: "user:9" });
  writeFileSync(path, `{"half":`, { flag: "a" });

  const second = new Ledger({ path });
  assert.equal(second.firstKind("mid:2"), "sent");
});

test("账：写不进去只 warn，内存里仍然记住（宁可丢账不能重复发）", () => {
  const dir = mkdtempSync(join(tmpdir(), "onebot-gate-"));
  const asFile = join(dir, "not-a-dir");
  writeFileSync(asFile, "x");
  const warns: string[] = [];

  const ledger = new Ledger({ path: join(asFile, "ledger.jsonl"), onWarn: (message) => warns.push(message) });
  ledger.appendNew({ kind: "sent", mode: "send", key: "mid:3", messageId: 3, scene: "group", target: "group:7" });

  assert.equal(warns.length, 1);
  assert.match(warns[0], /账写不进去/);
  assert.equal(ledger.firstKind("mid:3"), "sent");
});

test("scene 三值：只认有 id 的 group/private，其余一律 unknown", () => {
  assert.equal(sceneOf({ scene: "group", groupId: 700000001 }), "group");
  assert.equal(sceneOf({ scene: "private", userId: 3000000003 }), "private");
  // 缺 id / id 类型不对 / scene 认不出 / 根本不是对象，都只能落 unknown。
  assert.equal(sceneOf({ scene: "group" }), "unknown");
  assert.equal(sceneOf({ scene: "group", groupId: "700000001" }), "unknown");
  assert.equal(sceneOf({ scene: "group", groupId: Number.NaN }), "unknown");
  assert.equal(sceneOf({ scene: "private" }), "unknown");
  assert.equal(sceneOf({ scene: "private", userId: Number.POSITIVE_INFINITY }), "unknown");
  assert.equal(sceneOf({ scene: "channel", channelId: 1 }), "unknown");
  assert.equal(sceneOf({}), "unknown");
  assert.equal(sceneOf(null), "unknown");
  assert.equal(sceneOf(undefined), "unknown");
  assert.equal(sceneOf("group"), "unknown");

  // 第四个值永远不许出现：随便喂什么都只能落在这三个里。
  const samples: unknown[] = [{ scene: "group" }, { scene: "private" }, null, 42, [], { scene: "group", groupId: 1.5 }];
  for (const sample of samples) {
    assert.ok(["group", "private", "unknown"].includes(sceneOf(sample)), `不该出现别的值：${JSON.stringify(sample)}`);
  }
});

test("目标：unknown 就照实写 unknown，不写 group:undefined", () => {
  assert.equal(targetOf({ scene: "group", groupId: 7 }), "group:7");
  assert.equal(targetOf({ scene: "private", userId: 9 }), "user:9");
  assert.equal(targetOf({ scene: "group" }), "unknown");
  assert.equal(targetOf({ scene: "private", userId: "9" }), "unknown");
  assert.equal(targetOf(null), "unknown");
});

test("claim：同一 key 第一次 true、第二次 false，且是同步语义", () => {
  const ledger = new Ledger({ path: freshLedgerPath() });

  const first = ledger.claim("mid:1");
  assert.equal(typeof first, "boolean", "认领必须是同步布尔值，并发 relay 之间才谈得上互斥");
  assert.equal(first, true);
  assert.equal(ledger.claim("mid:1"), false);
  // 还没落账，但 firstKind 必须看得见它，否则日志会谎报成「已完成的某个 kind」。
  assert.equal(ledger.firstKind("mid:1"), "claimed");

  ledger.release("mid:1");
  assert.equal(ledger.firstKind("mid:1"), null);
  assert.equal(ledger.claim("mid:1"), true, "放掉之后必须能重新认领");
});

test("claim：历史账里已有的 key 直接拒绝，note 报「已有 kind=…」而不是在飞", () => {
  const path = freshLedgerPath();
  const before = new Ledger({ path });
  before.appendNew({ kind: "sent", mode: "send", key: "mid:9", messageId: 9, scene: "group", target: "group:7" });

  // 换实例 = 进程重启：在飞清零，历史账照旧拦住。
  const restarted = new Ledger({ path });
  assert.equal(restarted.claim("mid:9"), false);
  assert.equal(dedupNote(restarted.firstKind("mid:9")), "已有 kind=sent");
  assert.equal(restarted.claim("mid:9"), false, "被历史账拒绝时不许悄悄占住 key");
});

test("firstKind：真行优先，dup 只是旁证", () => {
  const ledger = new Ledger({ path: freshLedgerPath() });
  ledger.appendNew({ kind: "dup", mode: "send", key: "mid:5", messageId: 5, scene: "group", target: "group:7" });
  assert.equal(ledger.firstKind("mid:5"), "dup");

  ledger.appendNew({ kind: "sent", mode: "send", key: "mid:5", messageId: 5, scene: "group", target: "group:7" });
  assert.equal(ledger.firstKind("mid:5"), "sent", "真行要盖掉 dup");

  ledger.appendNew({ kind: "dup", mode: "send", key: "mid:5", messageId: 5, scene: "group", target: "group:7" });
  assert.equal(ledger.firstKind("mid:5"), "sent", "dup 不许把真行改小");
});

test("异常收尾的决定：只有还没落过真行时才补 error 行", () => {
  assert.equal(needsErrorRow(null), true);
  assert.equal(needsErrorRow("claimed"), true);
  assert.equal(needsErrorRow("sent"), false);
  assert.equal(needsErrorRow("error"), false);
  assert.equal(needsErrorRow("dup"), false);
});

test("认领后抛异常：补 1 行 error + 放掉在飞标记，不会把 key 永久锁死", () => {
  const path = freshLedgerPath();
  const ledger = new Ledger({ path });
  assert.equal(ledger.claim("mid:7"), true);

  // server 的 catch/finally 就是这个顺序：先看要不要补行，补完无条件 release。
  assert.equal(needsErrorRow(ledger.firstKind("mid:7")), true);
  ledger.appendNew({ kind: "error", mode: "send", key: "mid:7", messageId: 7, scene: "group", target: "group:7", note: "处理异常：boom" });
  ledger.release("mid:7");

  assert.equal(ledger.firstKind("mid:7"), "error", "在飞标记不该盖住真行");
  assert.equal(ledger.claim("mid:7"), false, "已有真行 → 之后按 duplicate 拦，不是锁死");

  // 反例：认领后一行没落就退出，release 之后必须能重新认领。
  assert.equal(ledger.claim("mid:8"), true);
  ledger.release("mid:8");
  assert.equal(ledger.firstKind("mid:8"), null);
  assert.equal(ledger.claim("mid:8"), true);
});
