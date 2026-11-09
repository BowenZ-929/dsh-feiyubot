// 定向判定：只有两档——「一定回」（定向）与「按预算回」（闲聊抽样）。
// 纯函数模块：不碰网络、不碰时钟、不碰文件——词表与数值由调用方注入（测试给字面量）。
//
// 第一层（硬拦）：命令 / 纯表情 / 纯图 —— 一律不回。
// 定向：私聊 / 被 @（@全体不算）/ 引用她自己发过的消息 / 文本里有她的外号 —— 一定回、不吃额度、不受 burst 约束。
// 其余：闲聊 —— 该群段序号 % chitchatSampleMod == 0 才可能回，且还要过 burst。
import { readFileSync } from "node:fs";

export type GateWords = {
  commandPrefixes: string[];
  nicknames: string[];
  burstMs: number;
  chitchatSampleMod: number;
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

const int = (raw: unknown, path: string, label: string): number => {
  const value = num(raw, path, label);
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`${label} ${path} 应为 >=1 的整数，实际是${show(raw)}`);
  }
  return value;
};

// 系数一律从这里读：代码里不许出现第二个数字。
export const readGateWords = (raw: unknown, label = "data/gate.json"): GateWords => {
  if (!isRecord(raw)) throw new Error(`${label} 应是 JSON 对象，实际是 ${show(raw)}`);
  return {
    commandPrefixes: words(raw.commandPrefixes, "commandPrefixes", label),
    nicknames: words(raw.nicknames, "nicknames", label),
    burstMs: num(raw.burstMs, "burstMs", label),
    chitchatSampleMod: int(raw.chitchatSampleMod, "chitchatSampleMod", label),
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

export type DirectRule = "private" | "at" | "quote" | "nickname" | "voice" | "continuation";
export type GateBlock = "command" | "sticker" | "media_only";

export type FirstLayerInput = {
  continuation?: boolean;
  scene: "group" | "private";
  mentionedSelf: boolean;
  quotedHer: boolean;
  text: string;
  isSticker: boolean;
  isBareImage: boolean;
};

export type FirstLayerVerdict =
  | { kind: "directed"; rule: DirectRule }
  | { kind: "blocked"; reason: GateBlock }
  | { kind: "chitchat" };

const startsWithAny = (text: string, prefixes: string[]): boolean => {
  const head = text.trimStart();
  return prefixes.some((prefix) => prefix.length > 0 && head.startsWith(prefix));
};

const hasAny = (text: string, needles: string[]): boolean =>
  needles.some((needle) => needle.length > 0 && text.includes(needle));

// 第一层：命令 > 定向（私聊/@/引用她/外号）> 纯表情/纯图 > 其余算闲聊。
export const firstLayer = (input: FirstLayerInput, gateWords: GateWords): FirstLayerVerdict => {
  // 本项目支持的语音口令，不能被「不接其它机器人的 / 命令」规则吞掉。
  if (/^\/语音(?:\s|$)/.test(input.text.trimStart())) return { kind: "directed", rule: "voice" };
  if (startsWithAny(input.text, gateWords.commandPrefixes)) return { kind: "blocked", reason: "command" };
  if (input.scene === "private") return { kind: "directed", rule: "private" };
  if (input.mentionedSelf) return { kind: "directed", rule: "at" };
  if (input.quotedHer) return { kind: "directed", rule: "quote" };
  if (hasAny(input.text, gateWords.nicknames)) return { kind: "directed", rule: "nickname" };
  if (input.continuation) return { kind: "directed", rule: "continuation" };
  if (input.isSticker) return { kind: "blocked", reason: "sticker" };
  if (input.isBareImage) return { kind: "blocked", reason: "media_only" };
  return { kind: "chitchat" };
};

// 确定性抽样：该群段序号能被 chitchatSampleMod 整除才「可能回」（还要再过 burst）。
// 不做随机——同一个序号永远同一个结果，账才能复盘。
export const chitchatSampled = (ordinal: number, gateWords: GateWords): boolean =>
  ordinal > 0 && ordinal % gateWords.chitchatSampleMod === 0;

// burst 阀：同一会话距上次真正发出不足 burstMs 就别接话。只对闲聊抽样生效；定向不受它约束。
export const burstBlocked = (lastSentAt: number | null, now: number, burstMs: number): boolean =>
  lastSentAt !== null && now - lastSentAt < burstMs;
