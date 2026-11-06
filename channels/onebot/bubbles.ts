// 换行 = 换泡。纯函数：不碰网络/时钟/文件。
//
// 判据（人拍，钉死）：
//   1. 一泡文本里有换行 → 按换行切开，各成一泡，各自带 send_at；
//   2. 切出来的泡仍受既有三道上限约束（那是 compose.ts 的事，这里只管切）；
//   3. 切出来是空串 / 纯空白 / 纯标点 → 不为它产生气泡（把那个换行去掉）；
//   4. 只有行尾一个换行 → 只发一泡，去掉行尾换行；
//   5. 泡数上限（chunks_per_reply）管住**最终发出去几个气泡**：超了就把后面的内容
//      合回最后一泡，一个字符都不许丢（合并时用 \n 拼回去）。
import type { Chunk } from "./outbox.ts";

export type SplitOptions = { gapSec: number; cap: number };
export type SplitResult = { chunks: Chunk[]; merged: boolean };

// 空 / 纯空白 / 纯标点：这种行不该占一个气泡。
// 只看标点（P*）与空白——emoji 之类算内容，不吞。
const isBlankLike = (text: string): boolean => {
  if (text.trim().length === 0) return true;
  return /^[\s\p{P}]+$/u.test(text);
};

export const splitBubbles = (chunks: Chunk[], options: SplitOptions): SplitResult => {
  const cap = Number.isFinite(options.cap) && options.cap >= 1 ? Math.floor(options.cap) : 1;
  const gapSec = Number.isFinite(options.gapSec) && options.gapSec > 0 ? options.gapSec : 0;
  const out: Chunk[] = [];
  let merged = false;

  for (const chunk of chunks) {
    // 只按 \n 切；\r 属于原文本内容，不擅自吞掉。
    const lines = chunk.text.split("\n").filter((line) => !isBlankLike(line));
    const parts = lines.length > 0 ? lines : [""];

    if (parts.length <= 1) {
      // 没有换行（或切完只剩一泡，比如只有行尾换行）：原样一泡。
      out.push({ text: parts[0], gap_before: chunk.gap_before, send_at: chunk.send_at });
      continue;
    }

    if (parts.length > cap) {
      merged = true;
      // 前 cap-1 泡各自出去，剩下的用 \n 拼回最后一泡：不截断、不少一个字符。
      const head = parts.slice(0, cap - 1);
      const tail = parts.slice(cap - 1).join("\n");
      for (let index = 0; index < head.length; index += 1) {
        out.push({
          text: head[index],
          gap_before: index === 0 ? chunk.gap_before : 0,
          send_at: chunk.send_at + index * gapSec,
        });
      }
      out.push({ text: tail, gap_before: 0, send_at: chunk.send_at + (cap - 1) * gapSec });
      continue;
    }

    for (let index = 0; index < parts.length; index += 1) {
      out.push({
        text: parts[index],
        gap_before: index === 0 ? chunk.gap_before : 0,
        send_at: chunk.send_at + index * gapSec,
      });
    }
  }

  return { chunks: out, merged };
};
