// 引用解析：把 reply 段的 message_id 换成「那句话」，号码绝不进她的文本。
// 纯函数（resolveQuote/renderQuote）+ 依赖注入（自己的环 / 入站环 / get_msg）——这里不碰网络与文件。
export type QuoteSource = "own" | "inbound" | "get_msg" | "none";
import { segmentToText } from "./parse.ts";
export type QuoteContent = { text: string | null; who: string; speaker: string };
export type QuoteResult = QuoteContent & { message_id: string; source: QuoteSource };

export type QuoteDeps = {
  own: (id: number) => QuoteContent | null;
  inbound: (id: number) => QuoteContent | null;
  // 调用方负责 2000ms 超时与「不重试」；这里只管它 resolve 出什么。
  getMsg: (id: number) => Promise<QuoteContent | null>;
};

// 顺序钉死：自己的环 → 最近入站环 → get_msg → none。
export const resolveQuote = async (id: number, deps: QuoteDeps): Promise<QuoteResult> => {
  const own = deps.own(id);
  if (own?.text) return { ...own, message_id: String(id), source: "own" };
  const inbound = deps.inbound(id);
  if (inbound?.text) return { ...inbound, message_id: String(id), source: "inbound" };
  const fetched = await deps.getMsg(id);
  if (fetched?.text) return { ...fetched, ...(own ? {who: own.who || fetched.who, speaker: own.speaker || fetched.speaker} : {}),
    message_id: String(id), source: "get_msg" };
  return { message_id: String(id), text: null, who: own?.who ?? "", speaker: own?.speaker ?? "", source: "none" };
};

// 注入形状：原文原样嵌（不截断、不改字）；取不到就一句人话，绝不留数字。
export const renderQuote = (quote: QuoteResult): string =>
  `（引用${quote.speaker || "作者未记录"}的消息：${quote.text === null ? "内容读不到" : `「${quote.text}」`}）`;

// get_msg 是外部边界：保留所有段的顺序，仅把真实返回的发送者作为作者。
export const quoteFromMsgData = (data: Record<string, unknown>): QuoteContent | null => {
  if (!Array.isArray(data.message)) return null;
  const text = data.message.map(segmentToText).join("");
  if (!text) return null;
  const sender = typeof data.sender === "object" && data.sender !== null ? data.sender as Record<string, unknown> : {};
  const rawId = sender.user_id;
  const who = ((typeof rawId === "number" && Number.isSafeInteger(rawId) && rawId > 0) ||
    (typeof rawId === "string" && /^[1-9]\d*$/.test(rawId))) ? String(rawId) : "";
  const name = [sender.card, sender.nickname].find(v => typeof v === "string" && v.trim()) as string | undefined;
  return { text, who, speaker: name ? name + (who ? `(${who})` : "") : who || "" };
};
