// 气泡切分：换行 = 换泡；一个气泡太长就只在标点处切（宁可长，不许断半句）。
// 纯函数：不碰网络/时钟/文件。
//
// 顺序钉死（不许跳级）：
//   ① 按 \n 切（换行=换泡）；
//   ② 泡内超 maxCharsPerBubble → 只在句末标点处切；
//   ③ 没有句末标点 → 只在逗号/顿号/分号处切；
//   ④ 连逗号都没有 → 不切；
//   ⑤ 切完拼回去必须逐字节等于原文。
// 每个 piece 带 sep（piece 之前被移除的那段原文）：换行切来的 sep="\n"，标点切来的 sep=""（标点留在左段末尾）。
// reconstruct = pieces.map(p => p.sep + p.text).join("") —— 合并回最后一泡时也按各自的 sep 还原，绝不统一用 "\n"。
import { createHash } from "node:crypto";
import type { Chunk } from "./outbox.ts";

export type BubblePolicy = {
  // 子泡之间的 send_at 步长（秒）。
  gapSec: number;
  // 最多几个气泡（chunks_per_reply，缺卡回落 maxChunks）。
  cap: number;
  // 单泡字数上限。
  maxChars: number;
  sentenceMarks: string[];
  clauseMarks: string[];
  tailParticles: string[];
  // 逗号粘连的长句按概率拆：命中率百分比 + 逗号字符表。
  commaSplitRatePct: number;
  commaSplitMarks: string[];
};

export type BubbleSplit = { reason: "bubble_len"; len: number; into: number[] };
export type CommaSplitRecord = { comma: string; len: number; into: [number, number] };
export type SplitResult = {
  chunks: Chunk[];
  merged: boolean;
  splits: BubbleSplit[];
  commaSplits: CommaSplitRecord[];
};

type Piece = { text: string; sep: string };

// 空 / 纯空白 / 纯标点：这种行不该占一个气泡。只看标点（P*）与空白——emoji 之类算内容，不吞。
const isBlankLike = (text: string): boolean => {
  if (text.trim().length === 0) return true;
  return /^[\s\p{P}]+$/u.test(text);
};

const charLen = (text: string): number => Array.from(text).length;

// 在这一堆标点里找「可以切」的最靠后位置：左段不能超长、右段非空、右段不能以语气词开头。
const lastCut = (text: string, marks: string[], particles: string[], maxChars: number): number => {
  let cut = -1;
  for (let index = 0; index < text.length; index += 1) {
    if (!marks.includes(text[index])) continue;
    const left = text.slice(0, index + 1);
    const right = text.slice(index + 1);
    if (right.length === 0) break;
    if (charLen(left) > maxChars) break;
    // 语气词保护：右段以语气词开头就不在这一刀切（继续往后找更后面的候选，或保留更早的候选）。
    if (particles.some((particle) => particle.length > 0 && right.startsWith(particle))) continue;
    cut = index + 1;
  }
  return cut;
};

// 确定性抽样：拿泡文本做 sha256，取前 8 位 hex 转 int，% 100 < 命中率 才拆。
// 同一句话永远同一个结论（可复现、账上可解释），绝不用 Math.random。
export const commaSplitHit = (text: string, ratePct: number): boolean => {
  if (!Number.isFinite(ratePct) || ratePct <= 0) return false;
  const hex = createHash("sha256").update(text, "utf8").digest("hex").slice(0, 8);
  return parseInt(hex, 16) % 100 < ratePct;
};

// 拆点：该泡里**最后一个**逗号；左段或右段为空/纯空白就往前找上一个。
// 返回切点（逗号的位置）与那个逗号本身；找不到返回 null。
const lastCommaCut = (text: string, marks: string[], particles: string[]): { at: number; comma: string } | null => {
  for (let index = text.length - 1; index >= 0; index -= 1) {
    const char = text[index];
    if (!marks.includes(char)) continue;
    const left = text.slice(0, index);
    const right = text.slice(index + 1);
    if (isBlankLike(left) || isBlankLike(right)) continue;
    // 语气词保护（与超长切分层同一条）：右段以语气词开头就不在这一刀切，往前找上一个逗号。
    if (particles.some((particle) => particle.length > 0 && right.startsWith(particle))) continue;
    return { at: index, comma: char };
  }
  return null;
};

const splitLong = (text: string, marks: string[], particles: string[], maxChars: number): string[] => {
  const pieces: string[] = [];
  let rest = text;
  while (charLen(rest) > maxChars) {
    const cut = lastCut(rest, marks, particles, maxChars);
    if (cut <= 0) break; // ④ 没有可用切点 → 不切
    pieces.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  pieces.push(rest);
  return pieces;
};

export const splitBubbles = (chunks: Chunk[], policy: BubblePolicy): SplitResult => {
  const cap = Number.isFinite(policy.cap) && policy.cap >= 1 ? Math.floor(policy.cap) : 1;
  const gapSec = Number.isFinite(policy.gapSec) && policy.gapSec > 0 ? policy.gapSec : 0;
  const maxChars = Number.isFinite(policy.maxChars) && policy.maxChars >= 1 ? Math.floor(policy.maxChars) : 1;
  const splits: BubbleSplit[] = [];
  const commaSplits: CommaSplitRecord[] = [];
  const out: Chunk[] = [];
  let merged = false;

  for (const chunk of chunks) {
    // 带图的一泡是原子的：不按换行/标点再切——切碎等于让图落在半句话旁边。
    // text 为空的只有图的泡也必须留下：这里直接产出，绕开下面「全空的不占泡」那一步。
    // image 原样透传（file/summary 一个字节都不改），顺序、gap_before、send_at 的算法与纯文字泡一致。
    if (chunk.image !== undefined || chunk.audio !== undefined) {
      out.push({
        text: chunk.text,
        ...(chunk.image === undefined ? {} : { image: chunk.image }),
        ...(chunk.audio === undefined ? {} : { audio: chunk.audio }),
        gap_before: chunk.gap_before,
        send_at: chunk.send_at,
      });
      continue;
    }

    // ① 换行切泡（sep 记下每段前面被移除的换行）
    const lines = chunk.text.split("\n");
    let pieces: Piece[] = [];
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      if (index > 0 && isBlankLike(line)) continue; // ③（M10 口径）空/纯空白/纯标点不产泡
      if (index === 0 && isBlankLike(line) && lines.length === 1) {
        pieces = [{ text: "", sep: "" }];
        break;
      }
      pieces.push({ text: line, sep: index === 0 ? "" : "\n" });
    }
    if (pieces.length === 0) pieces = [{ text: "", sep: "" }];

    // ②③ 泡内超长切（标点留在左段末尾，sep="")
    const expanded: Piece[] = [];
    for (const piece of pieces) {
      if (charLen(piece.text) <= maxChars) {
        expanded.push(piece);
        continue;
      }
      let parts = splitLong(piece.text, policy.sentenceMarks, policy.tailParticles, maxChars);
      if (parts.length === 1) {
        // 句末标点切不动（或根本没有）→ 退到逗号/顿号/分号
        parts = splitLong(piece.text, policy.clauseMarks, policy.tailParticles, maxChars);
      }
      if (parts.length > 1) {
        splits.push({ reason: "bubble_len", len: charLen(piece.text), into: parts.map((part) => charLen(part)) });
      }
      parts.forEach((part, index) => {
        expanded.push({ text: part, sep: index === 0 ? piece.sep : "" });
      });
    }

    // ④ 逗号粘连的长句：按概率拆两泡（丢弃那个逗号——这是「拼回逐字节相等」的唯一例外）
    const commaStage: Array<{ text: string; sep: string }> = [];
    for (const piece of expanded) {
      if (!commaSplitHit(piece.text, policy.commaSplitRatePct)) {
        commaStage.push(piece);
        continue;
      }
      const cut = lastCommaCut(piece.text, policy.commaSplitMarks, policy.tailParticles);
      if (cut === null) {
        commaStage.push(piece);
        continue;
      }
      const left = piece.text.slice(0, cut.at);
      const right = piece.text.slice(cut.at + 1);
      commaSplits.push({
        comma: cut.comma,
        len: charLen(piece.text),
        into: [charLen(left), charLen(right)],
      });
      commaStage.push({ text: left, sep: piece.sep });
      commaStage.push({ text: right, sep: "" });
    }
    expanded.length = 0;
    for (const piece of commaStage) expanded.push(piece);

    // 上限合并：超了就把后面的合回最后一泡（按各自 sep 还原，禁截断）
    if (expanded.length > cap) {
      merged = true;
      // 前面 cap-1 段原样保留；第 cap-1 段起全部合回最后一泡。
      // 合并时按每段的 sep 还原（换行来的 sep="\n"、标点来的 sep=""），绝不统一用 "\n"。
      const kept = expanded.slice(0, cap - 1);
      const tail = expanded[cap - 1];
      const restText = expanded
        .slice(cap)
        .map((piece) => piece.sep + piece.text)
        .join("");
      const mergedPiece: Piece = { text: tail.text + restText, sep: tail.sep };
      expanded.length = 0;
      for (const piece of kept) expanded.push(piece);
      expanded.push(mergedPiece);
    }

    for (let index = 0; index < expanded.length; index += 1) {
      const piece = expanded[index];
      if (piece.text.length === 0 && piece.sep.length === 0) continue; // 全空的不占泡
      out.push({
        text: piece.text,
        gap_before: index === 0 ? chunk.gap_before : 0,
        send_at: chunk.send_at + index * gapSec,
      });
    }
  }

  // 独立表情不合回文字；给它留一条位置。单条文字偏好仍允许另发一张表情。
  const separateImage = out.length > 1 && out.at(-1)!.image !== undefined && out.at(-1)!.text.trim() === ""
      && out.slice(0, -1).every(item => item.image === undefined && item.audio === undefined)
    ? out.pop() : undefined;
  const textCap = separateImage ? Math.max(1, cap - 1) : cap;
  // 核心可能已分成多泡；上限按整轮合并，不跨中途图片或语音。
  if (out.length > textCap && out.at(-1)!.audio === undefined
      && out.slice(0, -1).every(item => item.image === undefined && item.audio === undefined)) {
    const image = out.at(-1)!.image;
    out[textCap - 1].text = out.slice(textCap - 1).map(item => item.text).join("\n");
    if (image !== undefined) out[textCap - 1].image = image;
    out.splice(textCap);
    merged = true;
  }
  if (separateImage) {
    separateImage.send_at = Math.max(separateImage.send_at, out.at(-1)!.send_at + gapSec);
    out.push(separateImage);
  }
  return { chunks: out, merged, splits, commaSplits };
};
