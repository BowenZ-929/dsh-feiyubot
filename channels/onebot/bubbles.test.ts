import { test } from "node:test";
import assert from "node:assert/strict";
import { commaSplitHit, splitBubbles, type BubblePolicy } from "./bubbles.ts";
import type { Chunk } from "./outbox.ts";
import { buildAction, planChunks } from "./outbox.ts";
import { enforceLimits } from "./compose.ts";

const chunk = (text: string, send_at = 0): Chunk => ({ text, gap_before: 0, send_at });

// 测试用字面量（与 data/policy.json 同值）：口诀是「宁可长，不许断半句」。
const base: BubblePolicy = {
  gapSec: 0.8,
  cap: 3,
  maxChars: 60,
  sentenceMarks: ["。", "！", "？", "…", "；"],
  clauseMarks: ["，", "、", "；", ","],
  tailParticles: ["啊", "吧", "呢", "嘛", "呀", "哦", "啦", "咯", "喽", "呗", "噢"],
  // 逗号抽样拆：命中率 25%，只看 ，与 ,
  commaSplitRatePct: 25,
  commaSplitMarks: ["，", ","],
};
const opts = (patch: Partial<BubblePolicy> = {}): BubblePolicy => ({ ...base, ...patch });
// 换行切出来的泡：拼回去要把被去掉的换行补回来（"\n" 就是那个 sep）。
const joinNl = (chunks: Chunk[]): string => chunks.map((item) => item.text).join("\n");
// 标点切出来的泡：sep 是空串，直接拼就是原文。
const joinRaw = (chunks: Chunk[]): string => chunks.map((item) => item.text).join("");

test("整轮气泡上限：核心已分三句，逗号再拆后合回而不丢答案", () => {
  const result = splitBubbles([
    chunk("老大。"), chunk("叫法换了，我的答案还是这个。", 1), chunk("再问几遍也一样。", 2),
  ], opts());
  assert.equal(result.chunks.length, 3);
  assert.equal(result.merged, true);
  assert.equal(result.chunks.map(c => c.text).join("\n"), "老大。\n叫法换了\n我的答案还是这个。\n再问几遍也一样。");
});

test("语音不因朗读文字中的换行或长度被拆成重复音频", () => {
  const voice = { ...chunk("第一句话\n第二句话，很长很长。"), audio: { file: "file:///tmp/voice.wav" } };
  const result = splitBubbles([voice], opts({ maxChars: 2, commaSplitRatePct: 100 }));
  assert.deepEqual(result.chunks, [voice]);
});

test("聊天末尾配图：整轮合泡保留全部文字和唯一图片，支持一泡偏好", () => {
  const image = { file: "file:///tmp/rice.png", summary: "开饭啦" };
  for (const cap of [1, 3]) {
    const result = splitBubbles([
      chunk("给你留饭。\n坐这里。\n慢慢吃。"),
      { ...chunk("开饭啦。", 3), image },
    ], opts({ cap }));
    assert.equal(result.chunks.length, cap);
    assert.equal(result.merged, true);
    assert.equal(joinNl(result.chunks), "给你留饭。\n坐这里。\n慢慢吃。\n开饭啦。");
    assert.equal(result.chunks.filter(c => c.image).length, 1);
    assert.deepEqual(result.chunks.at(-1)?.image, image);
  }
});

test("换行 = 换泡：1 个换行切成 2 泡，各自 send_at", () => {
  const { chunks, merged } = splitBubbles([chunk("上句\n下句")], opts());
  assert.equal(chunks.length, 2);
  assert.deepEqual(chunks.map((item) => item.text), ["上句", "下句"]);
  assert.deepEqual(chunks.map((item) => item.send_at), [0, 0.8]);
  assert.equal(merged, false);
});

test("独立表情：合泡留出图片位置，群聊和私聊实际协议消息不混合图文", () => {
  const input = [chunk("第一句\n第二句\n第三句"),
    { ...chunk(""), image: { file: "file:///tmp/meme.png" } }];
  const before = structuredClone(input);
  for (const cap of [1, 3]) {
    const split = splitBubbles(input, opts({ cap, commaSplitRatePct: 0 }));
    const limited = enforceLimits(split.chunks, "meme-separate", {
      outbound: { maxChars: 512, maxSentences: 8, maxChunks: 3, defaultReplies: ["备用回复"] },
    });
    assert.equal(limited.reason, null);
    assert.equal(limited.chunks.length, cap === 1 ? 2 : 3);
    assert.equal(limited.chunks.slice(0, -1).map(c => c.text).join("\n"), "第一句\n第二句\n第三句");
    const last = limited.chunks.at(-1)!;
    assert.equal(last.text, "");
    assert.equal(last.send_at, limited.chunks.at(-2)!.send_at + 0.8);
    for (const where of [{ scene: "group", groupId: 700000001 },
                         { scene: "private", userId: 3000000003 }] as const) {
      const frames = planChunks(limited.chunks, 0).map(p => buildAction(where, p.text, String(p.index), p.image));
      assert.deepEqual(frames.at(-1)!.params.message.map(s => s.type), ["image"]);
      assert.ok(frames.slice(0, -1).every(f => f.params.message.every(s => s.type === "text")));
    }
  }
  assert.deepEqual(input, before, "不能原地修改核心回包");
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

// ---------- M13：逗号粘连的长句按概率拆 ----------

// 这两句的 sha256 前 8 位 % 100 分别是 19 与 30（25% 命中率下：一句拆、一句不拆）。
const COMMA_HIT = "小肥鱼 今天天气不错，我们去走走吧";
const COMMA_MISS = "小肥鱼 这家的面很好吃，下次带你来";

test("确定性抽样：同一句话永远同一个结论（连算两次一致）", () => {
  assert.equal(commaSplitHit(COMMA_HIT, 25), true);
  assert.equal(commaSplitHit(COMMA_HIT, 25), true);
  assert.equal(commaSplitHit(COMMA_MISS, 25), false);
  assert.equal(commaSplitHit(COMMA_MISS, 25), false);
  // 命中率 0 就一句都不拆
  assert.equal(commaSplitHit(COMMA_HIT, 0), false);
});

test("逗号拆：命中 → 两泡，丢弃那个逗号（拼回 == 原文去掉这一个逗号）", () => {
  const { chunks, commaSplits } = splitBubbles([chunk(COMMA_HIT)], opts());
  assert.equal(chunks.length, 2);
  assert.equal(chunks[0].text, "小肥鱼 今天天气不错");
  assert.ok(!chunks[0].text.endsWith("，"));
  assert.equal(chunks[1].text, "我们去走走吧");
  assert.equal(chunks.map((item) => item.text).join("，"), COMMA_HIT);
  assert.deepEqual(commaSplits, [{ comma: "，", len: Array.from(COMMA_HIT).length, into: [10, 6] }]);
  assert.deepEqual(chunks.map((item) => item.send_at), [0, 0.8]);
});

test("逗号拆：未命中 → 一泡，commaSplit 为空", () => {
  const { chunks, commaSplits } = splitBubbles([chunk(COMMA_MISS)], opts());
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].text, COMMA_MISS);
  assert.deepEqual(commaSplits, []);
});

test("逗号拆也守语气词保护：逗号紧接「啊」不切（rate=100 强制必拆也一样）", () => {
  const text = "甲".repeat(40) + "，啊" + "乙".repeat(30);
  const { chunks, commaSplits } = splitBubbles([chunk(text)], opts({ commaSplitRatePct: 100 }));
  // 这泡里只有一个逗号，而且右段以「啊」开头 → 不拆（整泡原样）
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].text, text);
  assert.deepEqual(commaSplits, []);
  // 断言口径：没有任何泡以语气词开头
  assert.ok(chunks.every((item) => !["啊", "吧", "呢", "嘛"].some((particle) => item.text.startsWith(particle))));
});

test("逗号拆只丢一个逗号；换行/超长仍逐字节可还原", () => {
  const text = "甲，乙，丙";
  const { chunks, commaSplits } = splitBubbles([chunk(text)], opts({ commaSplitRatePct: 100 }));
  // 命中率 100 → 一定拆；拆点取最后一个逗号
  assert.deepEqual(chunks.map((item) => item.text), ["甲，乙", "丙"]);
  assert.equal(commaSplits.length, 1);
  assert.equal(chunks.map((item) => item.text).join("，"), text);
  // 换行与超长切分的可还原性不受影响
  const nl = splitBubbles([chunk("一\n二\n三")], opts({ commaSplitRatePct: 100 }));
  assert.equal(nl.chunks.map((item) => item.text).join("\n"), "一\n二\n三");
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

