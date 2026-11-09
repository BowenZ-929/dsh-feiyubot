// 「她说过的话」：协议端 ack 里回的 data.message_id + 那一泡实际发出去的原文。
// 用途两个：① 判断一条 reply 引用的是不是**她自己发过的**消息（引用别人不算定向）；
//           ② 命中时能直接拿到她自己说的原文（号码不许进她的文本）。
// 环形上限 500 条，落 .state/own.json，原子写（临时文件 + rename），写盘失败只 warn。
// 旧格式 {ids:[...]} 仍要能读——读成「有 id、没有原文」。
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export const OWN_IDS_RELATIVE_PATH = ".state/own.json";
export const OWN_IDS_CAP = 500;

export type OwnEntry = { id: number; text: string | null; at: string | null };

export type OwnIdsOptions = {
  path: string;
  cap?: number;
  onWarn?: (message: string) => void;
};

export class OwnIds {
  readonly path: string;
  private readonly cap: number;
  private readonly onWarn: (message: string) => void;
  private entries: OwnEntry[];
  private readonly seen = new Set<number>();
  private readonly texts = new Map<number, string>();

  constructor(options: OwnIdsOptions) {
    this.path = options.path;
    this.cap = options.cap ?? OWN_IDS_CAP;
    this.onWarn = options.onWarn ?? (() => {});
    this.entries = this.load();
    for (const entry of this.entries) {
      this.seen.add(entry.id);
      if (entry.text !== null && entry.text.length > 0) this.texts.set(entry.id, entry.text);
    }
  }

  private load(): OwnEntry[] {
    if (!existsSync(this.path)) return [];
    let raw: unknown;
    try {
      raw = JSON.parse(readFileSync(this.path, "utf8"));
    } catch (error) {
      this.onWarn(`她说过的话读不出来（${this.path}）：${(error as Error).message}，本轮从空集合开始`);
      return [];
    }
    if (typeof raw !== "object" || raw === null) return [];
    const record = raw as Record<string, unknown>;
    const entries: OwnEntry[] = [];
    // 新格式：entries:[{id,text,at}]
    if (Array.isArray(record.entries)) {
      for (const item of record.entries) {
        if (typeof item !== "object" || item === null) continue;
        const row = item as Record<string, unknown>;
        if (typeof row.id !== "number" || !Number.isInteger(row.id)) continue;
        entries.push({
          id: row.id,
          text: typeof row.text === "string" && row.text.length > 0 ? row.text : null,
          at: typeof row.at === "string" ? row.at : null,
        });
      }
      return entries.slice(-this.cap);
    }
    // 旧格式：ids:[1,2,3] —— 读成「有 id、没有原文」，语义不变（仍然算她说过）
    if (Array.isArray(record.ids)) {
      for (const item of record.ids) {
        if (typeof item === "number" && Number.isInteger(item)) entries.push({ id: item, text: null, at: null });
      }
      return entries.slice(-this.cap);
    }
    return [];
  }

  has(id: number): boolean {
    return this.seen.has(id);
  }

  // 拿她自己说的原文：旧格式的条目没有原文，返回 null（调用方再去看入站环 / get_msg）。
  textOf(id: number): string | null {
    return this.texts.get(id) ?? null;
  }

  // 记一条「她说过的话」：id 必有；text/at 能给就给。
  add(id: number, text: string | null = null, at: string | null = null): void {
    if (!Number.isInteger(id)) return;
    if (this.seen.has(id)) {
      // 同一个 id 后来又拿到了原文：补上，不占新位置。
      if (text !== null && text.length > 0) {
        this.texts.set(id, text);
        const existing = this.entries.find((entry) => entry.id === id);
        if (existing !== undefined && existing.text === null) existing.text = text;
        this.persist();
      }
      return;
    }
    this.seen.add(id);
    if (text !== null && text.length > 0) this.texts.set(id, text);
    this.entries.push({ id, text: text !== null && text.length > 0 ? text : null, at });
    // 超上限时把最老的挪出去，同时把它从 set/map 里删掉（否则集合会一直涨）。
    while (this.entries.length > this.cap) {
      const dropped = this.entries.shift();
      if (dropped !== undefined) {
        this.seen.delete(dropped.id);
        this.texts.delete(dropped.id);
      }
    }
    this.persist();
  }

  size(): number {
    return this.entries.length;
  }

  // 临时文件 + rename：半截写的 JSON 会让下次启动整份集合作废。
  private persist(): void {
    const tmp = `${this.path}.tmp`;
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      writeFileSync(tmp, `${JSON.stringify({ entries: this.entries })}\n`, "utf8");
      renameSync(tmp, this.path);
    } catch (error) {
      this.onWarn(`她说过的话写不进去（${this.path}）：${(error as Error).message}，内存集合继续`);
    }
  }
}
