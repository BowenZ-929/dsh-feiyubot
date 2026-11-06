// 系数一律从 data/policy.json 读，代码里一个都不写死：这些数字是产品口径，会被反复调，
// 写进代码就只能靠改代码来调，而且调完之后没人分得清哪个是"默认值"、哪个是"当初拍的"。
// 这里只负责读盘与形状校验；读不出来就抛一条人看得懂的错——静默回落等于把系数变成隐藏常量。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export type Tier = "quietOpen" | "activeOpen" | "sameSpeaker" | "switchSpeaker";

export const TIERS: readonly Tier[] = ["quietOpen", "activeOpen", "sameSpeaker", "switchSpeaker"];

export type BatchPolicy = {
  quietGapMs: number;
  tiersMs: Record<Tier, number>;
  hardCapMs: number;
  mentionCapMs: number;
  bareImageMs: number;
  stickerMs: number;
  privateCapMs: number;
};

export type OutboundPolicy = {
  maxChars: number;
  maxSentences: number;
  maxChunks: number;
  defaultReplies: string[];
};

export type QuotaPolicy = {
  windowMs: number;
  perGroupMax: number;
  mentionCanExceed: boolean;
};

export type DecidePolicy = {
  reasonCodes: string[];
};

export type Policy = {
  batch: BatchPolicy;
  outbound: OutboundPolicy;
  quota: QuotaPolicy;
  decide: DecidePolicy;
};

// 按模块位置解析，而不是 process.cwd()：从 tools/ 或仓库根起进程时也该读到同一份系数。
export const DEFAULT_POLICY_PATH = fileURLToPath(new URL("./data/policy.json", import.meta.url));

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const show = (value: unknown): string => (value === undefined ? "（缺失）" : JSON.stringify(value));

const ms = (raw: unknown, path: string, label: string): number => {
  if (typeof raw !== "number" || !Number.isFinite(raw)) {
    throw new Error(`${label} ${path} 应为有限数字，实际是${show(raw)}`);
  }
  if (raw < 0) throw new Error(`${label} ${path} 不能是负数（${raw}）`);
  return raw;
};

const count = (raw: unknown, path: string, label: string): number => {
  const value = ms(raw, path, label);
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`${label} ${path} 应为 >=1 的整数，实际是${show(raw)}`);
  }
  return value;
};

const flag = (raw: unknown, path: string, label: string): boolean => {
  if (typeof raw !== "boolean") throw new Error(`${label} ${path} 应为布尔，实际是${show(raw)}`);
  return raw;
};

const lines = (raw: unknown, path: string, label: string): string[] => {
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new Error(`${label} ${path} 应为非空数组，实际是${show(raw)}`);
  }
  return raw.map((item, index) => {
    if (typeof item !== "string" || item.trim().length === 0) {
      throw new Error(`${label} ${path}[${index}] 应为非空字符串，实际是${show(item)}`);
    }
    return item;
  });
};

const section = (raw: Record<string, unknown>, name: string, label: string): Record<string, unknown> => {
  const value = raw[name];
  if (!isRecord(value)) throw new Error(`${label} 缺少 ${name} 段（应为对象），实际是${show(value)}`);
  return value;
};

// 每个键都必须显式存在：少一个键就在读盘这一刻炸掉，而不是等到半夜某条消息进来才发现等错了时长。
export const readPolicy = (raw: unknown, label = "data/policy.json"): Policy => {
  if (!isRecord(raw)) throw new Error(`${label} 应是 JSON 对象，实际是 ${show(raw)}`);

  const batch = section(raw, "batch", label);
  const tiersRaw = section(batch, "tiersMs", label);
  const tiersMs = {} as Record<Tier, number>;
  for (const tier of TIERS) {
    tiersMs[tier] = ms(tiersRaw[tier], `batch.tiersMs.${tier}`, label);
  }

  const outbound = section(raw, "outbound", label);
  const quota = section(raw, "quota", label);
  const decide = section(raw, "decide", label);

  return {
    batch: {
      quietGapMs: ms(batch.quietGapMs, "batch.quietGapMs", label),
      tiersMs,
      hardCapMs: ms(batch.hardCapMs, "batch.hardCapMs", label),
      mentionCapMs: ms(batch.mentionCapMs, "batch.mentionCapMs", label),
      bareImageMs: ms(batch.bareImageMs, "batch.bareImageMs", label),
      stickerMs: ms(batch.stickerMs, "batch.stickerMs", label),
      privateCapMs: ms(batch.privateCapMs, "batch.privateCapMs", label),
    },
    outbound: {
      maxChars: count(outbound.maxChars, "outbound.maxChars", label),
      maxSentences: count(outbound.maxSentences, "outbound.maxSentences", label),
      maxChunks: count(outbound.maxChunks, "outbound.maxChunks", label),
      defaultReplies: lines(outbound.defaultReplies, "outbound.defaultReplies", label),
    },
    quota: {
      windowMs: ms(quota.windowMs, "quota.windowMs", label),
      perGroupMax: count(quota.perGroupMax, "quota.perGroupMax", label),
      mentionCanExceed: flag(quota.mentionCanExceed, "quota.mentionCanExceed", label),
    },
    decide: {
      reasonCodes: lines(decide.reasonCodes, "decide.reasonCodes", label),
    },
  };
};

export const loadPolicy = (path: string = DEFAULT_POLICY_PATH): Policy => {
  let content: string;
  try {
    content = readFileSync(path, "utf8");
  } catch (error) {
    throw new Error(`读不到系数文件 ${path}：${(error as Error).message}`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch (error) {
    throw new Error(`系数文件 ${path} 不是合法 JSON：${(error as Error).message}`);
  }
  return readPolicy(parsed, path);
};
