// 门禁：壳的浏览器半边只许 webServer.register + 面板，不许出现 DSH 的 agent / session 调用。
//
// 口径（先 grep 过才定的）：
//   grep -riE '\bagent\b|session|dsh-agent|dsh-session' shell/src
// 现在是 0 命中——连 session 也没有：轮询优化用的是 document.visibilityState，
// 注释里写的是中文「会话可见性」，英文 session 一次都没出现。所以这里就是纯断言。
//
// 将来若真有一处无害命中（比如某条注释里的英文 session），口径是把它**逐字**写进下面的
// ALLOW 并说明理由；不许为了过测试去改无关的注释，也不许把 BANNED 放宽成通配。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "src");

const BANNED = [
  /dsh-agent/i,
  /dsh-session/i,
  /\bagent\b/i,
  /session/i,
];

// 逐字允许的行（目前一条都没有）
const ALLOW = [];

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(path));
    else if (entry.isFile()) out.push(path);
  }
  return out;
}

test("shell/src 里不出现 DSH 的 agent/session 调用", () => {
  const hits = [];
  for (const path of walk(SRC)) {
    const lines = readFileSync(path, "utf8").split("\n");
    lines.forEach((line, index) => {
      if (ALLOW.some((allowed) => line.includes(allowed))) return;
      for (const pattern of BANNED) {
        if (pattern.test(line)) {
          hits.push(`${relative(SRC, path)}:${index + 1}: ${line.trim()}`);
          break;
        }
      }
    });
  }
  assert.deepEqual(hits, [], "壳只许 webServer 与面板：\n" + hits.join("\n"));
});
