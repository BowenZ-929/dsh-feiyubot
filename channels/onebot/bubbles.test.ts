import { test } from "node:test";
import assert from "node:assert/strict";
import { splitBubbles } from "./bubbles.ts";
import type { Chunk } from "./outbox.ts";

const chunk = (text: string, send_at = 0): Chunk => ({ text, gap_before: 0, send_at });

test("换行 = 换泡：1 个换行切成 2 泡，各自 send_at", () => {
  const { chunks, merged } = splitBubbles([chunk("上句\n下句")], { gapSec: 0.8, cap: 3 });
  assert.equal(chunks.length, 2);
  assert.deepEqual(chunks.map((item) => item.text), ["上句", "下句"]);
  assert.deepEqual(chunks.map((item) => item.send_at), [0, 0.8]);
  assert.equal(merged, false);
});

test("行尾只有换行：只发一泡，且不含行尾换行", () => {
  const { chunks } = splitBubbles([chunk("只有一行\n")], { gapSec: 0.8, cap: 3 });
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].text, "只有一行");
});

test("空行 / 纯空白 / 纯标点不产生气泡", () => {
  const { chunks } = splitBubbles([chunk("第一句\n \n……\n第三句")], { gapSec: 0.8, cap: 3 });
  assert.deepEqual(chunks.map((item) => item.text), ["第一句", "第三句"]);
});

test("超出 chunks_per_reply：后面的内容用 \n 合回最后一泡，一个字符都不丢", () => {
  const text = "a\nb\nc\nd\ne\nf";
  const { chunks, merged } = splitBubbles([chunk(text)], { gapSec: 0.8, cap: 3 });
  assert.equal(merged, true);
  assert.equal(chunks.length, 3);
  assert.deepEqual(chunks.map((item) => item.text), ["a", "b", "c\nd\ne\nf"]);
  // 逐字节拼回去 == 原文（换行是唯一被当作分隔符的东西，没有丢字）
  assert.equal(chunks.map((item) => item.text).join("\n"), text);
});

test("父 send_at 是基准：子泡 send_at = 父 + i × gap", () => {
  const { chunks } = splitBubbles([chunk("一\n二\n三", 1.5)], { gapSec: 0.8, cap: 3 });
  assert.deepEqual(chunks.map((item) => item.send_at), [1.5, 2.3, 3.1]);
});

test("没有换行就原样一泡（不凭空多出气泡）", () => {
  const { chunks, merged } = splitBubbles([chunk("一句话")], { gapSec: 0.8, cap: 3 });
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].text, "一句话");
  assert.equal(merged, false);
});
