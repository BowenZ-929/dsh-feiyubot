// 定向判定：这一条消息到底是不是在叫她。
// 纯函数模块：不碰网络、不碰时钟、不碰文件——词表与阈值由调用方注入（测试给字面量）。
//
// 三层结构里这里只管「定向」这一件事：
//   第一层（布尔链）：命令 / 私聊 / @ / 引用她 / 纯表情 / 纯图
//   第二层 (a)(b)（便宜判，不用调核）：问号+人称、外号
//   第二层 (c)（要等核回包）：recalled 里的最高分 ≥ 阈值
import { readFileSync } from "node:fs";

export type GateWords = {
  commandPrefixes: string[];
  questionMarks: string[];
  personWords: string[];
  nicknames: string[];
  burstMs: number;
  recalledMinScore: number;
};

export const DEFAULT_GATE_PATH = new URL("./data/gate.json", import.meta.url).pathname;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const show = (value: unknown): string => (value === undefined ? "（缺失）" : JSON.stringify(value));

const words = (raw: unknown, path: string, label: string): string[] => {
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new Error(`${label} ${path} 应为非空数组，实际是${show(raw)}`);
  }
  return raw.map((item, index) => {
    if (typeof item !== "string" || item.length === 0) {
      throw new Error(`${label} ${path}[${index}] 应为非空字符串，实际是${show(item)}`);
    }
    return item;
  });
};

const num = (raw: unknown, path: string, label: string): number => {
  if (typeof raw !== "number" || !Number.isFinite(raw)) {
    throw new Error(`${label} ${path} 应为有限数字，实际是${show(raw)}`);
  }
  if (raw < 0) throw new Error(`${label} ${path} 不能是负数（${raw}）`);
  return raw;
};

// 系数一律从这里读：代码里不许出现第二个数字（阈值也一样）。
export const readGateWords = (raw: unknown, label = "data/gate.json"): GateWords => {
  if (!isRecord(raw)) throw new Error(`${label} 应是 JSON 对象，实际是 ${show(raw)}`);
  return {
    commandPrefixes: words(raw.commandPrefixes, "commandPrefixes", label),
    questionMarks: words(raw.questionMarks, "questionMarks", label),
    personWords: words(raw.personWords, "personWords", label),
    nicknames: words(raw.nicknames, "nicknames", label),
    burstMs: num(raw.burstMs, "burstMs", label),
    recalledMinScore: num(raw.recalledMinScore, "recalledMinScore", label),
  };
};

export const loadGateWords = (path: string = DEFAULT_GATE_PATH): GateWords => {
  let content: string;
  try {
    content = readFileSync(path, "utf8");
  } catch (error) {
    throw new Error(`读不到判定词表 ${path}：${(error as Error).message}`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch (error) {
    throw new Error(`判定词表 ${path} 不是合法 JSON：${(error as Error).message}`);
  }
  return readGateWords(parsed, path);
};

export type DirectRule = "private" | "at" | "quote" | "question+person" | "nickname" | "recalled";
export type DirectBlock = "command" | "sticker" | "media_only" | "no_mention";

export type FirstLayerInput = {
  scene: "group" | "private";
  mentionedSelf: boolean;
  quotedHer: boolean;
  text: string;
  isSticker: boolean;
  isBareImage: boolean;
};

export type FirstLayerVerdict =
  | { kind: "directed"; rule: "private" | "at" | "quote" }
  | { kind: "blocked"; reason: "command" | "sticker" | "media_only" }
  | { kind: "undecided" };

const startsWithAny = (text: string, prefixes: string[]): boolean => {
  const head = text.trimStart();
  return prefixes.some((prefix) => prefix.length > 0 && head.startsWith(prefix));
};

const hasAny = (text: string, needles: string[]): boolean =>
  needles.some((needle) => needle.length > 0 && text.includes(needle));

// 第一层：布尔链。顺序照人拍的来：命令 > 定向（私聊/@/引用）> 纯表情/纯图。
export const firstLayer = (input: FirstLayerInput, gateWords: GateWords): FirstLayerVerdict => {
  if (startsWithAny(input.text, gateWords.commandPrefixes)) return { kind: "blocked", reason: "command" };
  if (input.scene === "private") return { kind: "directed", rule: "private" };
  if (input.mentionedSelf) return { kind: "directed", rule: "at" };
  if (input.quotedHer) return { kind: "directed", rule: "quote" };
  if (input.isSticker) return { kind: "blocked", reason: "sticker" };
  if (input.isBareImage) return { kind: "blocked", reason: "media_only" };
  return { kind: "undecided" };
};

export type SecondLayerVerdict = { rule: "question+person" | "nickname" } | null;

// 第二层 (a)(b)：先便宜判，不用调核。a 先于 b（问号+人称比「只提外号」更明确）。
export const secondLayer = (text: string, gateWords: GateWords): SecondLayerVerdict => {
  if (hasAny(text, gateWords.questionMarks) && hasAny(text, gateWords.personWords)) {
    return { rule: "question+person" };
  }
  if (hasAny(text, gateWords.nicknames)) return { rule: "nickname" };
  return null;
};

// 第二层 (c)：核回的 recalled 里最大的 score。没有 recalled / 没有 score 就是 null（不是 0）。
export const readRecalledTopScore = (payload: unknown): number | null => {
  if (!isRecord(payload)) return null;
  const recalled = payload.recalled;
  if (!Array.isArray(recalled)) return null;
  let top: number | null = null;
  for (const item of recalled) {
    if (!isRecord(item)) continue;
    const score = item.score;
    if (typeof score !== "number" || !Number.isFinite(score)) continue;
    if (top === null || score > top) top = score;
  }
  return top;
};

// (c) 的判据：最高分 ≥ 阈值才算「核记得她说的事，值得搭话」。
// 阈值未经数据校准（data/gate.json 里写明是占位值），所以决策账必须把它一起记下来。
export const recalledPass = (topScore: number | null, minScore: number): boolean =>
  topScore !== null && topScore >= minScore;

// burst 阀：同一会话距上次真正发出不足 burstMs 就别接话（窗口按会话各算）。
export const burstBlocked = (lastSentAt: number | null, now: number, burstMs: number): boolean =>
  lastSentAt !== null && now - lastSentAt < burstMs;
