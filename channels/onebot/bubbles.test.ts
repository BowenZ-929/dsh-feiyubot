import { test } from "node:test";
import assert from "node:assert/strict";
import { splitBubbles, type BubblePolicy } from "./bubbles.ts";
import type { Chunk } from "./outbox.ts";

const chunk = (text: string, send_at = 0): Chunk => ({ text, gap_before: 0, send_at });

// 测试用字面量（与 data/policy.json 同值）：口诀是「宁可长，不许断半句」。
const base: BubblePolicy = {
  gapSec: 0.8,
  cap: 3,
  maxChars: 60,
  sentenceMarks: ["。", "！", "？", "…", "；"],
  clauseMarks: ["，", "、", "；", ","],
  tailParticles: ["啊", "吧", "呢", "嘛", "呀", "哦", "啦", "咯", "喽", "呗", "噢"],
};
const opts = (patch: Partial<BubblePolicy> = {}): BubblePolicy => ({ ...base, ...patch });
// 换行切出来的泡：拼回去要把被去掉的换行补回来（"\n" 就是那个 sep）。
const joinNl = (chunks: Chunk[]): string => chunks.map((item) => item.text).join("\n");
// 标点切出来的泡：sep 是空串，直接拼就是原文。
const joinRaw = (chunks: Chunk[]): string => chunks.map((item) => item.text).join("");

test("换行 = 换泡：1 个换行切成 2 泡，各自 send_at", () => {
  const { chunks, merged } = splitBubbles([chunk("上句\n下句")], opts());
  assert.equal(chunks.length, 2);
  assert.deepEqual(chunks.map((item) => item.text), ["上句", "下句"]);
  assert.deepEqual(chunks.map((item) => item.send_at), [0, 0.8]);
  assert.equal(merged, false);
});

test("行尾只有换行：只发一泡，且不含行尾换行", () => {
  const { chunks } = splitBubbles([chunk("只有一行\n")], opts());
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].text, "只有一行");
});

test("空行 / 纯空白 / 纯标点不产生气泡", () => {
  const { chunks } = splitBubbles([chunk("第一句\n \n……\n第三句")], opts());
  assert.deepEqual(chunks.map((item) => item.text), ["第一句", "第三句"]);
});

test("超出 chunks_per_reply：后面的内容按 sep 合回最后一泡，拼回 == 原文", () => {
  const text = "a\nb\nc\nd\ne\nf";
  const { chunks, merged } = splitBubbles([chunk(text)], opts());
  assert.equal(merged, true);
  assert.equal(chunks.length, 3);
  assert.deepEqual(chunks.map((item) => item.text), ["a", "b", "c\nd\ne\nf"]);
  assert.equal(joinNl(chunks), text);
});

test("父 send_at 是基准：子泡 send_at = 父 + i × gap", () => {
  const { chunks } = splitBubbles([chunk("一\n二\n三", 1.5)], opts());
  assert.deepEqual(chunks.map((item) => item.send_at), [1.5, 2.3, 3.1]);
});

test("没有换行就原样一泡（不凭空多出气泡）", () => {
  const { chunks, merged } = splitBubbles([chunk("一句话")], opts());
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].text, "一句话");
  assert.equal(merged, false);
});

// ---------- M11：单泡字数上限 ----------

test("② 超长但有句末标点：只在句末切开，每泡 ≤ 上限，拼回 == 原文", () => {
  const a = "甲".repeat(48) + "。";
  const b = "乙".repeat(48) + "！";
  const c = "丙".repeat(48) + "？";
  const text = a + b + c;
  const { chunks, splits } = splitBubbles([chunk(text)], opts());
  assert.equal(chunks.length, 3);
  assert.deepEqual(chunks.map((item) => item.text), [a, b, c]);
  assert.ok(chunks.every((item) => Array.from(item.text).length <= 60));
  assert.equal(joinRaw(chunks), text);
  assert.deepEqual(splits, [{ reason: "bubble_len", len: Array.from(text).length, into: [49, 49, 49] }]);
});

test("③ 没有句末标点但有逗号：在逗号处切成 2 泡，拼回 == 原文", () => {
  const left = "字".repeat(40) + "，";
  const right = "词".repeat(39);
  const text = left + right;
  const { chunks } = splitBubbles([chunk(text)], opts());
  assert.equal(chunks.length, 2);
  assert.deepEqual(chunks.map((item) => item.text), [left, right]);
  assert.equal(joinRaw(chunks), text);
});

test("④ 连逗号都没有：不切（宁可一泡长，也不许断半句）", () => {
  const text = "字".repeat(150);
  const { chunks, splits } = splitBubbles([chunk(text)], opts());
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].text, text);
  assert.deepEqual(splits, []);
});

test("语气词保护：切点右边是「啊」就不在那儿切，把切点往前挪", () => {
  // 第一个逗号右边是「啊……」→ 不许切；第二个逗号才是有效切点
  const first = "甲".repeat(40) + "，啊" + "乙".repeat(10) + "，";
  const tail = "丙".repeat(10);
  const text = first + tail;
  const { chunks } = splitBubbles([chunk(text)], opts());
  assert.equal(chunks.length, 2);
  // 「啊」必须留在第一个泡里
  assert.equal(chunks[0].text, first);
  assert.ok(chunks[0].text.includes("啊"));
  assert.equal(joinRaw(chunks), text);
});

test("切开后又因 chunks_per_reply 合并：按各自 sep 还原，拼回仍 == 原文", () => {
  const text = "一\n二\n三\n四\n五";
  const { chunks, merged } = splitBubbles([chunk(text)], opts({ cap: 2 }));
  assert.equal(merged, true);
  assert.equal(chunks.length, 2);
  assert.equal(joinNl(chunks), text);
});

// ---------- 带图的泡：一张图的那一泡是原子的 ----------

const imageChunk = (
  text: string,
  image: { file: string; summary?: string },
  send_at = 0,
  gap_before = 0,
): Chunk => ({ text, image, gap_before, send_at });

const PNG = "file:///tmp/onebot-selfportrait.png";

test("带图：只有图的泡（text 空）留下来，且 image 原样带上", () => {
  const { chunks, merged, splits } = splitBubbles([imageChunk("", { file: PNG, summary: "一张占位图" })], opts());

  assert.equal(chunks.length, 1, "只有图的泡不许被当成空泡吃掉");
  assert.deepEqual(chunks[0], {
    text: "",
    image: { file: PNG, summary: "一张占位图" },
    gap_before: 0,
    send_at: 0,
  });
  assert.equal(merged, false);
  assert.deepEqual(splits, [], "没切过，就不该记一次 bubble_len");
});

test("带图：text 里有换行也整泡透传，不切（图不落在半句话旁边）", () => {
  const text = "一句话\n第二句";
  const { chunks } = splitBubbles([imageChunk(text, { file: PNG })], opts());

  assert.equal(chunks.length, 1, "带图的泡不按换行切");
  assert.equal(chunks[0].text, text, "原文一个字都不许改");
  assert.deepEqual(chunks[0].image, { file: PNG });
});

test("带图：超长 text 也不按标点切（不切就是原样一泡）", () => {
  const text = "甲".repeat(48) + "。" + "乙".repeat(48) + "！";
  const { chunks, splits } = splitBubbles([imageChunk(text, { file: PNG, summary: "长文带图" })], opts());

  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].text, text);
  assert.deepEqual(chunks[0].image, { file: PNG, summary: "长文带图" });
  assert.deepEqual(splits, []);
});

test("回归：纯文字泡照旧按换行切，且产出里不多出 image 键", () => {
  const { chunks } = splitBubbles([chunk("上句\n下句")], opts());

  assert.deepEqual(chunks.map((item) => item.text), ["上句", "下句"]);
  assert.deepEqual(chunks.map((item) => item.send_at), [0, 0.8]);
  assert.ok(chunks.every((item) => !("image" in item)), "纯文字泡不许凭空多一个 image 键");
});

test("混合批次：顺序不变，带图泡整泡透传，send_at/gap_before 算法照旧", () => {
  const { chunks } = splitBubbles(
    [
      chunk("甲\n乙", 1),
      imageChunk("", { file: PNG, summary: "图一" }, 2.5, 0.4),
      chunk("丙", 4),
    ],
    opts(),
  );

  assert.deepEqual(
    chunks.map((item) => [item.text, item.send_at, item.gap_before, item.image === undefined ? null : item.image.file]),
    [
      ["甲", 1, 0, null],
      ["乙", 1.8, 0, null],
      ["", 2.5, 0.4, PNG],
      ["丙", 4, 0, null],
    ],
  );
  // 顺序 = 入参顺序：带图那一泡仍排在甲的下一泡之后、丙之前。
  assert.match(chunks[2].image?.summary ?? "", /^图一$/);
});

