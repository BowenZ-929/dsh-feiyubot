// 守门用例：演练工具与测试**不许**指向生产端口 / 生产状态目录。
// 背景：验收脚本反复往生产端口与生产账本目录上跑，把她的账删了、把她的嘴堵了。
// 这里把「演练只许用 6299 + /tmp/ch-drill」变成一条会红的断言。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
// 探测串用片段拼出来：这个文件自己不许被自己的规则扫中（否则守门用例永远红）。
const PROD_PORT = "62" + "00";
const STATE_NAME = "." + "state";
const STATE_QUOTED = '"' + STATE_NAME + '"';
const STATE_PATH = STATE_NAME + "/";

const scanned = (): Array<{ name: string; text: string }> => {
  const files: Array<{ name: string; text: string }> = [];
  for (const name of [join("tools", "drill.ts"), join("tools", "fake-client.ts")]) {
    files.push({ name, text: readFileSync(join(HERE, name), "utf8") });
  }
  for (const entry of readdirSync(HERE)) {
    if (!entry.endsWith(".test.ts")) continue;
    if (entry === "hygiene.test.ts") continue; // 跳过自己
    files.push({ name: entry, text: readFileSync(join(HERE, entry), "utf8") });
  }
  return files;
};

test("守门：演练工具与测试里不许出现生产端口", () => {
  for (const file of scanned()) {
    if (file.text.includes(PROD_PORT)) {
      assert.fail(`${file.name} 里出现了生产端口（${PROD_PORT}）：演练必须用 6299`);
    }
  }
});

test("守门：演练工具与测试里不许出现生产状态目录的字面路径", () => {
  for (const file of scanned()) {
    if (file.text.includes(STATE_QUOTED) || file.text.includes(STATE_PATH)) {
      assert.fail(`${file.name} 里出现了生产状态目录的字面路径（${STATE_NAME}）：演练必须用 --state /tmp/ch-drill`);
    }
  }
});

test("守门：drill 的默认值是演练专用端口与演练专用状态目录", () => {
  const source = readFileSync(join(HERE, "tools", "drill.ts"), "utf8");
  assert.match(source, /const DRILL_PORT = 6299;/, "drill 的默认端口必须是 6299");
  assert.match(source, /const DRILL_STATE = "\/tmp\/ch-drill";/, "drill 的默认状态目录必须是 /tmp/ch-drill");
  // 默认地址必须从那个端口派生，不许再写死第二个端口号
  assert.match(source, /const DRILL_URL = "ws:\/\/127\.0\.0\.1:" \+ DRILL_PORT \+ "\/ws";/);
  // ledger / decisions / quota 都必须挂在 --state 下面
  assert.match(source, /join\(state, "ledger\.jsonl"\)/);
  assert.match(source, /join\(state, "decisions\.jsonl"\)/);
  assert.match(source, /join\(state, "quota\.json"\)/);
});

test("守门：自己起的通道必须先试演练端口 6299", () => {
  const source = readFileSync(join(HERE, "tools", "drill.ts"), "utf8");
  // 自起通道那一段里，DRILL_PORT 必须先于 freePort() 出现：先按人拍的 6299 起，被占才回退。
  const section = source.slice(source.indexOf("const startChannel"));
  assert.ok(section.length > 0, "drill.ts 里必须还有 startChannel");
  assert.ok(section.includes("DRILL_PORT"), "startChannel 必须先试 DRILL_PORT（6299）");
  assert.ok(
    section.indexOf("DRILL_PORT") < section.indexOf("freePort()"),
    "DRILL_PORT 必须排在 freePort() 之前（先是 6299，被占才回退）",
  );
  assert.match(section, /被占，演练改用/, "回退到空闲端口时必须打一行说明，不许悄悄换");
});

test("守门：自己起的通道必须把 --state 传下去", () => {
  const source = readFileSync(join(HERE, "tools", "drill.ts"), "utf8");
  assert.match(source, /"--state",\s*\n\s*stateDir,/, "startChannel 必须把 --state 传给子进程");
});
