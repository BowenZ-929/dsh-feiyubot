import { test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { Quota, QUOTA_RELATIVE_PATH } from "./quota.ts";
import type { QuotaLedgerPolicy } from "./quota.ts";

// data/policy.json 的冻结值：1 小时窗口，每群 6 次，@ 可突破。
const policy: QuotaLedgerPolicy = { quota: { windowMs: 3_600_000, perGroupMax: 6, mentionCanExceed: true } };

const HOUR = 3_600_000;
const T0 = 1_760_000_000_000;

const freshPath = (): string => join(mkdtempSync(join(tmpdir(), "onebot-quota-")), QUOTA_RELATIVE_PATH);

const fill = (quota: Quota, key: string, count: number): void => {
  for (let i = 0; i < count; i += 1) quota.charge(key, T0 + i * 1000, false);
};

test("配额：同一群连打 7 次，第 7 次 ok=false、used=6", () => {
  const quota = new Quota({ path: freshPath(), policy });
  const results = [];
  for (let i = 0; i < 7; i += 1) results.push(quota.charge("group:7", T0 + i * 1000, false));
  assert.deepEqual(
    results.map((r) => [r.ok, r.used, r.overridden]),
    [
      [true, 1, false],
      [true, 2, false],
      [true, 3, false],
      [true, 4, false],
      [true, 5, false],
      [true, 6, false],
      [false, 6, false],
    ],
  );
});

test("配额：被 @ 的那次即使超了也 ok=true 且 overridden=true", () => {
  const quota = new Quota({ path: freshPath(), policy });
  fill(quota, "group:7", 6);
  assert.deepEqual(
    quota.charge("group:7", T0 + 7000, true),
    { ok: true, used: 7, overridden: true },
    "第 7 次被 @ 照样回，并记 overridden",
  );
  assert.deepEqual(quota.charge("group:7", T0 + 8000, true), { ok: true, used: 8, overridden: true });
  assert.deepEqual(
    quota.charge("group:7", T0 + 9000, false),
    { ok: false, used: 8, overridden: false },
    "突破只对被点名那次有效，没点名照旧拦",
  );
});

test("配额：另一个群不受影响（冷却单位是群，不是进程）", () => {
  const quota = new Quota({ path: freshPath(), policy });
  for (let i = 0; i < 7; i += 1) quota.charge("group:7", T0 + i * 1000, false);
  assert.equal(quota.charge("group:7", T0 + 8000, false).ok, false);
  assert.deepEqual(quota.charge("group:8", T0 + 8000, false), { ok: true, used: 1, overridden: false });
  assert.deepEqual(quota.charge("group:9", T0 + 8000, false), { ok: true, used: 1, overridden: false });
});

test("配额：滑窗过期后恢复", () => {
  const quota = new Quota({ path: freshPath(), policy });
  fill(quota, "group:7", 6); // T0 .. T0+5000
  assert.equal(quota.charge("group:7", T0 + HOUR - 1, false).ok, false, "窗口内仍然拦住");

  const partial = new Quota({ path: freshPath(), policy });
  fill(partial, "group:7", 6);
  assert.deepEqual(
    partial.charge("group:7", T0 + HOUR + 2000, false),
    { ok: true, used: 4, overridden: false },
    "T0/T0+1000/T0+2000 滑出窗口，剩三条 + 刚记的一次",
  );

  const expired = new Quota({ path: freshPath(), policy });
  fill(expired, "group:7", 6);
  assert.deepEqual(
    expired.charge("group:7", T0 + HOUR + 5500, false),
    { ok: true, used: 1, overridden: false },
    "全部滑出后从头计",
  );
});

test("配额：私聊不记账，used 恒为 0", () => {
  const path = freshPath();
  const quota = new Quota({ path, policy });
  for (let i = 0; i < 20; i += 1) {
    assert.deepEqual(quota.charge("user:9", T0 + i, false), { ok: true, used: 0, overridden: false });
  }
  assert.equal(quota.charge("unknown", T0, false).used, 0);
  // 私聊一个字都不写盘：这里先让群记一次账，文件里就只该有群那个键。
  assert.equal(existsSync(path), false, "只打私聊时连配额文件都不该被创建");
  quota.charge("group:1", T0, false);
  const state = JSON.parse(readFileSync(path, "utf8"));
  assert.deepEqual(Object.keys(state.groups), ["group:1"], "私聊不许在配额文件里留下任何键");
});

test("配额：状态文件形如 {groups:{'group:7':[毫秒整数]}}，重启后接着数", () => {
  const path = freshPath();
  const quota = new Quota({ path, policy });
  quota.charge("group:7", T0, false);
  quota.charge("group:7", T0 + 1000, false);
  const state = JSON.parse(readFileSync(path, "utf8"));
  assert.deepEqual(state, { groups: { "group:7": [T0, T0 + 1000] } });
  assert.ok(state.groups["group:7"].every((ts: number) => Number.isInteger(ts)), "ts 必须是毫秒整数");

  const revived = new Quota({ path, policy });
  assert.deepEqual(revived.charge("group:7", T0 + 2000, false), { ok: true, used: 3, overridden: false });
});

test("配额：状态文件写不进去只 warn 不抛，内存计数仍然正确", () => {
  const path = freshPath();
  const warns: string[] = [];
  const quota = new Quota({ path, policy, onWarn: (m) => warns.push(m) });
  fill(quota, "group:7", 6);
  assert.equal(warns.length, 0);

  const dir = dirname(path);
  chmodSync(dir, 0o500); // 只有读+执行：临时文件都建不出来
  try {
    assert.deepEqual(
      quota.charge("group:7", T0 + 7000, false),
      { ok: false, used: 6, overridden: false },
      "写盘失败不影响内存里的判定",
    );
    assert.equal(warns.length, 1);
    assert.match(warns[0], /配额状态写不进去/);
    assert.match(warns[0], /EACCES|permission denied/i);
    assert.deepEqual(
      quota.charge("group:7", T0 + 8000, true),
      { ok: true, used: 7, overridden: true },
      "内存计数继续往上走",
    );
    assert.equal(warns.length, 2);
  } finally {
    chmodSync(dir, 0o700);
  }
});

test("配额：坏掉的状态文件从零开始数并 warn", () => {
  const path = freshPath();
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, "{ 这不是 JSON", "utf8");
  const warns: string[] = [];
  const quota = new Quota({ path, policy, onWarn: (m) => warns.push(m) });
  assert.deepEqual(quota.charge("group:7", T0, false), { ok: true, used: 1, overridden: false });
  assert.equal(warns.length, 1);
  assert.match(warns[0], /配额状态读不出来/);
});
