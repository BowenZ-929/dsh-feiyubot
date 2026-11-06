// 出站的「一条泡里带张图」：分泡不被丢、帧里段序 text→image、两条入口（/say 与核回包）共用同一份字段判据。
// 图片路径一律用 /tmp 下的占位：这里不发真图，也不许出现真实账号。
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  IMAGE_SUMMARY_MAX,
  buildAction,
  parseChunkParts,
  parseCoreResponse,
  planChunks,
  runBatch,
} from "./outbox.ts";
import type { BatchInput, Chunk } from "./outbox.ts";
import { makeRow, type LedgerRow, type Mode } from "./gate.ts";

const PNG = "file:///tmp/onebot-image-a.png";

const chunk = (text: string, image?: { file: string; summary?: string }): Chunk =>
  image === undefined ? { text, gap_before: 0, send_at: 0 } : { text, image, gap_before: 0, send_at: 0 };

// 只做「发出去这一帧长什么样」需要的那几个假件：不碰网络、不碰真账、不睡真时间。
const harness = (plan: ReturnType<typeof planChunks>, mode: Mode = "send") => {
  const frames: Array<Record<string, unknown>> = [];
  const rows: LedgerRow[] = [];
  const logs: string[] = [];
  const sentFrames: string[] = [];
  const input: BatchInput = {
    mode,
    batch: { key: "say-1", messageId: null, scene: "group", target: "group:700000001" },
    where: { scene: "group", groupId: 700000001 },
    plan,
    requestSeq: 1,
    ledger: {
      appendNew: (row) => {
        rows.push(makeRow(row, "2026-01-01T00:00:00.000Z"));
        return row;
      },
    },
    isGateOpen: () => mode === "send",
    isSocketOpen: () => true,
    send: (frame) => {
      sentFrames.push(frame);
      frames.push(JSON.parse(frame));
    },
    waitAck: async (echo) => ({ kind: "ack", status: "ok", retcode: 0, messageId: null, note: `echo=${echo}` }),
    sleep: async () => {},
    now: () => 0,
    log: (tag, message) => {
      logs.push(`${tag} ${message}`);
    },
  };
  return { input, frames, rows, logs, sentFrames };
};

test("分泡：只有图的泡不被丢（text 空 + image 有），序号连续不留洞", () => {
  const plan = planChunks([chunk("先"), chunk("", { file: PNG }), chunk("后")], 1000);

  assert.equal(plan.length, 3, "只有图的泡也是一个泡");
  assert.deepEqual(plan.map((entry) => entry.index), [0, 1, 2]);
  assert.equal(plan[1].text, "");
  assert.deepEqual(plan[1].image, { file: PNG });
  // send_at 规则一个字没动。
  assert.deepEqual(plan.map((entry) => entry.sendAt), [1000, 1000, 1000]);
});

test("分泡：无 text 且无 image 的泡照旧丢掉（空泡判据只是放宽成「两个都空才丢」）", () => {
  const plan = planChunks([chunk("   "), chunk(""), chunk("真话")], 0);
  assert.deepEqual(plan.map((entry) => entry.text), ["真话"]);
  assert.deepEqual(plan.map((entry) => entry.index), [0]);
});

test("action：一泡里两者都有时 text 在前、image 在后；只有图时只有一个 image 段", () => {
  const where = { scene: "group", groupId: 700000001 } as const;
  const both = buildAction(where, "看这个", "req-1-0", { file: PNG, summary: "一张占位图" });
  assert.deepEqual(both, {
    action: "send_group_msg",
    params: {
      group_id: 700000001,
      message: [
        { type: "text", data: { text: "看这个" } },
        { type: "image", data: { file: PNG, summary: "一张占位图" } },
      ],
    },
    echo: "req-1-0",
  });

  const onlyImage = buildAction(where, "", "req-1-1", { file: PNG });
  assert.deepEqual(onlyImage, {
    action: "send_group_msg",
    params: { group_id: 700000001, message: [{ type: "image", data: { file: PNG } }] },
    echo: "req-1-1",
  });

  // 私聊同理：action 名与 where 的选择不因为带图而变。
  const privateImage = buildAction({ scene: "private", userId: 3000000003 }, "", "req-1-2", { file: PNG });
  assert.equal(privateImage.action, "send_private_msg");
  assert.deepEqual(privateImage.params, {
    user_id: 3000000003,
    message: [{ type: "image", data: { file: PNG } }],
  });
});

test("字段判据：file 不以 file:// 开头（或缺 file）→ 拒，文案说得清第几个泡的哪个字段", () => {
  for (const bad of [
    { text: "看图", image: { file: "/tmp/onebot-image-a.png" } },
    { text: "看图", image: { file: "http://example.invalid/a.png" } },
    { text: "看图", image: { file: "" } },
    { text: "看图", image: { file: "file://" } },
    { text: "看图", image: { summary: "只有替身没有图" } },
    { text: "看图", image: "file:///tmp/onebot-image-a.png" },
  ]) {
    const parts = parseChunkParts(bad, "chunks[2]");
    assert.equal(parts.ok, false, `应当拒：${JSON.stringify(bad)}`);
    if (!parts.ok) {
      assert.match(parts.message, /^chunks\[2\]\.image(\.file)? /);
      assert.match(parts.message, /file:\/\/ 开头的绝对路径|必须是对象/);
    }
  }
  // 反例：file:// 开头的非空字符串就放行。
  const good = parseChunkParts({ text: "", image: { file: PNG } }, "chunks[0]");
  assert.equal(good.ok, true);
});

test("字段判据：/say 那边 text 空且没 image → 拒；text 空但 image 有 → 放行", () => {
  // rejectEmpty=true 就是 /say 的收口：请求写错了当场 400，别等到分泡时被无声丢掉。
  for (const bad of [{ text: "" }, { text: "   " }, {}, { image: undefined }]) {
    const parts = parseChunkParts(bad, "chunks[1]", true);
    assert.equal(parts.ok, false, `应当拒：${JSON.stringify(bad)}`);
    if (!parts.ok) assert.match(parts.message, /^chunks\[1\] 里 text 与 image 至少要有一个/);
  }

  const emptyWithImage = parseChunkParts({ text: "", image: { file: PNG } }, "chunks[1]", true);
  assert.equal(emptyWithImage.ok, true);
  if (emptyWithImage.ok) {
    assert.equal(emptyWithImage.value.text, "");
    assert.deepEqual(emptyWithImage.value.image, { file: PNG });
  }

  const noTextKey = parseChunkParts({ image: { file: PNG } }, "chunks[1]", true);
  assert.equal(noTextKey.ok, true, "不写 text 只给 image 也算有意义的一泡");

  // text 必须是字符串（可以为空串）。
  const notString = parseChunkParts({ text: 42, image: { file: PNG } }, "chunks[1]", true);
  assert.equal(notString.ok, false);
  if (!notString.ok) assert.match(notString.message, /^chunks\[1\]\.text 必须是字符串/);
});

test("核的回包不拒空白泡：既有判据一个字没动（空白泡仍然走 empty，不是 error）", () => {
  // rejectEmpty 缺省关闭：核回一个空白泡，仍要能认出来，由分泡判成「她这轮没说」。
  const parts = parseChunkParts({ text: "" }, "chunks[1]");
  assert.equal(parts.ok, true);
  if (parts.ok) assert.deepEqual(parts.value, { text: "" });

  const reply = parseCoreResponse({ ok: true, chunks: [{ text: "", gap_before: 0, send_at: 0 }] });
  assert.equal(reply.ok, true);
  if (reply.ok) {
    assert.deepEqual(reply.chunks, [{ text: "", gap_before: 0, send_at: 0 }]);
    assert.deepEqual(planChunks(reply.chunks, 0), []);
  }
});

test("字段判据：summary 超 80 字符 → 拒，正好 80 → 放行", () => {
  assert.equal(IMAGE_SUMMARY_MAX, 80);

  const over = parseChunkParts({ text: "看图", image: { file: PNG, summary: "字".repeat(81) } }, "chunks[3]");
  assert.equal(over.ok, false);
  if (!over.ok) {
    assert.match(over.message, /^chunks\[3\]\.image\.summary 最多 80 个字符（拿到 81 个）/);
  }

  const at = parseChunkParts({ text: "看图", image: { file: PNG, summary: "字".repeat(80) } }, "chunks[3]");
  assert.equal(at.ok, true);

  // 按码点算：40 个 emoji 也是 40 个字，不是 80。
  const emoji = parseChunkParts({ text: "看图", image: { file: PNG, summary: "🌒".repeat(40) } }, "chunks[3]");
  assert.equal(emoji.ok, true);

  const notString = parseChunkParts({ text: "看图", image: { file: PNG, summary: 7 } }, "chunks[3]");
  assert.equal(notString.ok, false);
  if (!notString.ok) assert.match(notString.message, /^chunks\[3\]\.image\.summary 必须是字符串/);
});

test("核的回包认 image：chunks 里的 image 原样进 Chunk，没有 image 的泡不多出空洞的键", () => {
  const reply = parseCoreResponse({
    ok: true,
    chunks: [
      { text: "给你看", image: { file: PNG, summary: "一张占位图" }, gap_before: 0.5, send_at: 1.5 },
      { image: { file: PNG }, gap_before: 0, send_at: 2 },
    ],
  });
  assert.equal(reply.ok, true);
  if (!reply.ok) return;
  assert.deepEqual(reply.chunks[0], {
    text: "给你看",
    image: { file: PNG, summary: "一张占位图" },
    gap_before: 0.5,
    send_at: 1.5,
  });
  assert.deepEqual(reply.chunks[1], { text: "", image: { file: PNG }, gap_before: 0, send_at: 2 });

  const textOnly = parseCoreResponse({ ok: true, chunks: [{ text: "甲", send_at: 0 }] });
  assert.equal(textOnly.ok, true);
  if (textOnly.ok) assert.deepEqual(textOnly.chunks, [{ text: "甲", gap_before: 0, send_at: 0 }]);

  // 核回了一个坏 image：整轮认不出（不猜着发）。
  const broken = parseCoreResponse({ ok: true, chunks: [{ text: "甲", image: { file: "/tmp/a.png" } }] });
  assert.equal(broken.ok, false);
  if (!broken.ok) assert.match(broken.reason, /^chunks\[0\]\.image\.file 必须是 file:\/\/ 开头的绝对路径/);
});

test("runBatch 发只有图的那一泡：帧里只有一个 image 段，账上仍是一泡一行 sent", async () => {
  const run = harness(planChunks([chunk("", { file: PNG, summary: "一张占位图" })], 0));
  const result = await runBatch(run.input);

  assert.equal(result.sent, 1);
  assert.equal(run.sentFrames.length, 1);
  assert.deepEqual(run.frames[0], {
    action: "send_group_msg",
    params: {
      group_id: 700000001,
      message: [{ type: "image", data: { file: PNG, summary: "一张占位图" } }],
    },
    echo: "req-1-0",
  });
  assert.deepEqual(run.rows.map((row) => [row.kind, row.index, row.text]), [["sent", 0, ""]]);
  assert.ok(run.logs.some((line) => line.startsWith("chunk ") && line.includes("image=")));
});

test("runBatch 门挡住：带图的批次在 blocked 行的 note 里留下 image=N，纯文字批次一字不变", async () => {
  const withImage = harness(planChunks([chunk("想发的"), chunk("", { file: PNG })], 0), "silent");
  await runBatch(withImage.input);
  assert.equal(withImage.rows.length, 1);
  assert.equal(withImage.rows[0].kind, "blocked");
  assert.equal(withImage.rows[0].note, "chunks=2 send_at=[0,0] image=1");

  const textOnly = harness(planChunks([chunk("想发的"), chunk("再说")], 0), "silent");
  await runBatch(textOnly.input);
  assert.equal(textOnly.rows[0].note, "chunks=2 send_at=[0,0]");
});
