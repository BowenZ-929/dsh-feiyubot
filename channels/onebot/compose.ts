// 出站三道上限：字 / 句 / 条。
// 越界的处理是"整批换成一句短的"，不是截断——截断会留下半句，
// 那既不像人说话，也让账上记的那句话跟核真正想说的不是一回事。
import { createHash } from "node:crypto";
import type { Chunk } from "./outbox.ts";

// 只声明这里真正读到的键：policy 由调用方整份注入（测试里给字面量），
// 这样本文件既不知道 policy.json 在哪，也拿不到任何写死的系数。
export type ComposePolicy = {
  outbound: {
    maxChars: number;
    maxSentences: number;
    maxChunks: number;
    defaultReplies: string[];
  };
};

export type LimitReason = "core_empty" | "replaced_too_long" | null;
export type LimitResult = { chunks: Chunk[]; reason: LimitReason };

// 句数口径按标点切、空白段不算：末尾没标点也照样算一句，
// 否则"你好。今天天气不错"会被数成一句，上限就成了摆设。
const SENTENCE_BREAK = /[。！？!?…；;\n]+/u;

export const countSentences = (text: string): number =>
  text.split(SENTENCE_BREAK).filter((part) => part.trim().length > 0).length;

// 按码点算：一个 emoji 也是"一个字"，用 .length 会把代理对算成两个。
export const countChars = (text: string): number => Array.from(text).length;

// 同一个 batchId 必须永远选出同一句：重放、重试、重启后复盘时，
// 同一批在账上出现两个不同的替换句就等于自己给自己造了第二个事实。
export const pickDefaultReply = (batchId: string, defaultReplies: string[]): string => {
  const hex = createHash("sha256").update(batchId, "utf8").digest("hex");
  return defaultReplies[parseInt(hex.slice(0, 8), 16) % defaultReplies.length];
};

// 判定顺序无关紧要（任一越界都是同一个结局），所以这里只回答"越没越界"。
export const exceedsLimits = (chunks: Chunk[], policy: ComposePolicy): boolean => {
  const { maxChars, maxSentences, maxChunks } = policy.outbound;
  if (chunks.length > maxChunks) return true;
  return chunks.some(
    (chunk) => countChars(chunk.text) > maxChars || countSentences(chunk.text) > maxSentences,
  );
};

export const enforceLimits = (
  chunks: Chunk[],
  batchId: string,
  policy: ComposePolicy,
): LimitResult => {
  if (chunks.length === 0) return { chunks: [], reason: "core_empty" };
  if (!exceedsLimits(chunks, policy)) return { chunks, reason: null };
  // 策略里没备默认短句时绝不在这里现编一句：那等于让代码自己新增文案，
  // 而且没人 review 过。宁可这轮不发，账上落 core_empty。
  const { defaultReplies } = policy.outbound;
  if (defaultReplies.length === 0) return { chunks: [], reason: "core_empty" };
  // 整批换掉：一条，send_at=0（替换句说的是"现在这一下"，不该再等）。
  // 原文一个字都不留——半句比一句短话更像故障。
  return {
    chunks: [{ text: pickDefaultReply(batchId, defaultReplies), gap_before: 0, send_at: 0 }],
    reason: "replaced_too_long",
  };
};
