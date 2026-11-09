// 段渲染：把一批消息摊成核一眼能读的历史——每行一个人一个时间，原话一个字不改。
// 换行必须原样留着：压平之后两句话会粘成一句，核接的就是错误的话头。
import type { BatchSnapshot } from "./batch.ts";
import type { Tier } from "./policy.ts";
import type { Attachment } from "./parse.ts";
import { renderQuote, type QuoteResult } from "./quotes.ts";

export type SegmentMessage = {
  quote?: QuoteResult;
  id: number;
  name: string;
  text: string;
  at: string;
  mentioned: boolean;
};

export type SegmentBatch = {
  id: string;
  count: number;
  waited_ms: number;
  tier: Tier;
  mentioned: boolean;
  at: string;
  scene: "group" | "private";
  groupId?: number;
  userId?: number;
  msgs: SegmentMessage[];
};

const iso = (ms: number): string => new Date(ms).toISOString();

// 昵称缺失时退回 QQ：一行里没有人名，核就分不清这句是谁说的。
const speakerName = (msg: { id: number; name: string }): string =>
  msg.name.trim() === "" ? String(msg.id) : msg.name;

export const renderSegment = (batch: BatchSnapshot): {
  text: string;
  batch: SegmentBatch;
  segment: { who: string; speaker: string; text: string; quote?: QuoteResult }[];
  attachments: (Attachment & { speaker: string; speaker_id: string; segment_index: number; messageId: number | null })[];
} => {
  const msgs: SegmentMessage[] = batch.msgs.map((msg) => ({
    id: msg.id,
    name: speakerName(msg),
    // 原话里可能有换行，这里一个字都不碰：行内的 \n 是说话人自己断的句。
    text: msg.text,
    at: iso(msg.at),
    mentioned: msg.mentioned,
    ...(msg.quote ? { quote: msg.quote } : {}),
  }));

  const rendered: SegmentBatch = {
    id: batch.batchId,
    count: msgs.length,
    waited_ms: batch.waited_ms,
    tier: batch.tier,
    mentioned: batch.mentioned,
    at: iso(batch.firstAt),
    scene: batch.scene,
    msgs,
  };
  if (batch.groupId !== undefined) rendered.groupId = batch.groupId;
  if (batch.userId !== undefined) rendered.userId = batch.userId;

  // 每一条消息都先按自己内部的 \n 切成物理行，再给"每一行"都补上 昵称(id) 前缀——
  // 只给每条消息补一次的话，自带换行的原话会渲染出没有人名的续行，核就可能把续行当成别人说的话。
  // 空行照留（不丢、也不把连续两个换行压成一个）：那是说话人自己按下的回车。
  // 注意只有这里的 text 被摊平；batch.msgs[].text 仍然是原话原文，结构化那一半一条消息一个条目。
  const text = msgs
    .flatMap((msg) => (msg.quote ? renderQuote(msg.quote) + "\n" + msg.text : msg.text).split("\n").map((line) => `${msg.name}(${msg.id})：${line}`))
    .join("\n");

  const segment = msgs.map((msg) => ({ who: String(msg.id), speaker: `${msg.name}(${msg.id})`, text: msg.text,
    ...(msg.quote ? { quote: msg.quote } : {}) }));
  const attachments = batch.msgs.flatMap((msg, index) => (msg.attachments ?? []).map((item) => ({
    ...item, speaker: `${speakerName(msg)}(${msg.id})`, speaker_id: String(msg.id), segment_index: index, messageId: msg.messageId ?? null,
  })));
  return { text, batch: rendered, segment, attachments };
};
