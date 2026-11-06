// 家的偏好卡（只读）：<home>/home/名片盒/prefs.jsonl，按人折最新，取 params.chunks_per_reply。
// 纯函数 + 一个薄读盘壳：折卡不碰盘，读盘只读、一个字节都不写。
//
// 卡片的「人」写在 by / name / who 里（家的 store 用 by；别处可能用 who）——
// 三者都收，按出现顺序优先；统一折成字符串键（QQ 号也是字符串）。
import { existsSync, readFileSync, statSync } from "node:fs";

export type PrefsEntry = { chunksPerReply: number };
export type FoldResult = { prefs: Map<string, PrefsEntry>; skipped: number; invalid: number };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const asKey = (value: unknown): string | undefined => {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string" && value.trim().length > 0) return value.trim();
  return undefined;
};

// 值只认 1–3 的整数（chunks_per_reply 的合法域）；其它一律当作「这张卡没这个键」。
const asCap = (value: unknown): number | undefined => {
  if (typeof value !== "number" || !Number.isInteger(value)) return undefined;
  if (value < 1 || value > 3) return undefined;
  return value;
};

// 一张卡是谁的：by 优先（家的 store 用这个），其次 who，最后 name。
const ownerOf = (card: Record<string, unknown>): string | undefined =>
  asKey(card.by) ?? asKey(card.who) ?? asKey(card.name);

// JSONL 只增不改：同一个 who 取**最后一行**；半行/坏行跳过并计数，绝不让整份失效。
export const foldPrefsStats = (jsonlText: string): FoldResult => {
  const prefs = new Map<string, PrefsEntry>();
  let skipped = 0;
  // 坏值（卡在、但 chunks_per_reply 不是 1–3 的整数）单独计数：这不是「坏行」，但同样要 warn。
  let invalid = 0;
  for (const line of jsonlText.split("\n")) {
    const trimmed = line.trim();
    if (trimmed.length === 0) continue;
    let value: unknown;
    try {
      value = JSON.parse(trimmed);
    } catch {
      skipped += 1; // 半行（正在写）或坏行
      continue;
    }
    if (!isRecord(value)) {
      skipped += 1;
      continue;
    }
    const who = ownerOf(value);
    if (who === undefined) {
      skipped += 1;
      continue;
    }
    const params = isRecord(value.params) ? value.params : {};
    const cap = asCap(params.chunks_per_reply);
    if (cap === undefined) {
      // 这个 who 有卡但没这个键（或值非法）：删掉旧值（最后一行说了算），回落交给 capFor。
      if (params.chunks_per_reply !== undefined) invalid += 1;
      prefs.delete(who);
      continue;
    }
    prefs.set(who, { chunksPerReply: cap });
  }
  return { prefs, skipped, invalid };
};

export const foldPrefs = (jsonlText: string): Map<string, PrefsEntry> => foldPrefsStats(jsonlText).prefs;

// 取卡：缺卡 / 没这个键 / 值非法 → fallback（默认就是 outbound.maxChunks）。
export const capFor = (who: string | number | null, prefs: Map<string, PrefsEntry>, fallback: number): number => {
  if (who === null) return fallback;
  const key = String(who);
  const entry = prefs.get(key);
  if (entry === undefined) return fallback;
  return asCap(entry.chunksPerReply) ?? fallback;
};

export type PrefsStore = {
  path: string;
  // 按文件 mtime 缓存：家盘只在 mtime 变了才重读，别每泡都读整份。
  read: () => { prefs: Map<string, PrefsEntry>; skipped: number; invalid: number; missing: boolean; warn: string | null };
};

export const openPrefsStore = (path: string): PrefsStore => {
  let cachedMtime: number | null = null;
  let cached = {
    prefs: new Map<string, PrefsEntry>(),
    skipped: 0,
    invalid: 0,
    missing: true,
    warn: null as string | null,
  };
  return {
    path,
    read: () => {
      if (!existsSync(path)) {
        cachedMtime = null;
        cached = { prefs: new Map(), skipped: 0, invalid: 0, missing: true, warn: null };
        return cached;
      }
      let mtime: number;
      try {
        mtime = statSync(path).mtimeMs;
      } catch (error) {
        cached = {
          prefs: new Map(),
          skipped: 0,
          invalid: 0,
          missing: true,
          warn: `偏好卡 stat 失败（${path}）：${(error as Error).message}`,
        };
        return cached;
      }
      if (cachedMtime === mtime) return cached;
      try {
        const text = readFileSync(path, "utf8");
        const folded = foldPrefsStats(text);
        cachedMtime = mtime;
        cached = {
          prefs: folded.prefs,
          skipped: folded.skipped,
          invalid: folded.invalid,
          missing: false,
          warn:
            folded.skipped > 0 || folded.invalid > 0
              ? `偏好卡有 ${folded.skipped} 行坏行 / ${folded.invalid} 个非法值（已跳过，该 who 回落 maxChunks）：${path}`
              : null,
        };
        return cached;
      } catch (error) {
        cached = {
          prefs: new Map(),
          skipped: 0,
          invalid: 0,
          missing: true,
          warn: `偏好卡读不出来（${path}）：${(error as Error).message}`,
        };
        return cached;
      }
    },
  };
};
