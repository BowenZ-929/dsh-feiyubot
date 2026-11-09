// 最近入站环：.state/inbound.json，环形 500 条 {id, who, text, at}。
// 引用原文与同群旁听共用此环；旧无target记录不猜会话归属。
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { Policy } from './policy.ts';

export const INBOUND_RELATIVE_PATH = ".state/inbound.json";
export const INBOUND_CAP = 500;

export type InboundEntry = { id: number; who: number; name?: string; text: string; at: string; target?: string };

export type InboundRingOptions = { path: string; cap?: number; onWarn?: (message: string) => void };

export class InboundRing {
  readonly path: string;
  private readonly cap: number;
  private readonly onWarn: (message: string) => void;
  private entries: InboundEntry[];
  private readonly byId = new Map<number, InboundEntry>();

  constructor(options: InboundRingOptions) {
    this.path = options.path;
    this.cap = options.cap ?? INBOUND_CAP;
    this.onWarn = options.onWarn ?? (() => {});
    this.entries = this.load();
    for (const entry of this.entries) this.byId.set(entry.id, entry);
  }

  private load(): InboundEntry[] {
    if (!existsSync(this.path)) return [];
    let raw: unknown;
    try {
      raw = JSON.parse(readFileSync(this.path, "utf8"));
    } catch (error) {
      this.onWarn(`入站环读不出来（${this.path}）：${(error as Error).message}，本轮从空环开始`);
      return [];
    }
    if (typeof raw !== "object" || raw === null) return [];
    const list = (raw as Record<string, unknown>).entries;
    if (!Array.isArray(list)) return [];
    const entries: InboundEntry[] = [];
    for (const item of list) {
      if (typeof item !== "object" || item === null) continue;
      const record = item as Record<string, unknown>;
      if (typeof record.id !== "number" || !Number.isInteger(record.id)) continue;
      if (typeof record.text !== "string" || record.text.length === 0) continue;
      entries.push({
        id: record.id,
        who: typeof record.who === "number" ? record.who : 0,
        ...(typeof record.name === "string" ? { name: record.name } : {}),
        ...(typeof record.target === "string" ? { target: record.target } : {}),
        text: record.text,
        at: typeof record.at === "string" ? record.at : "",
      });
    }
    return entries.slice(-this.cap);
  }

  textOf(id: number): string | null {
    return this.byId.get(id)?.text ?? null;
  }

  quoteOf(id: number): { text: string; who: string; speaker: string } | null {
    const entry = this.byId.get(id);
    if (!entry) return null;
    const who = Number.isSafeInteger(entry.who) && entry.who > 0 ? String(entry.who) : "";
    return { text: entry.text, who, speaker: entry.name ? entry.name + (who ? `(${who})` : "") : who };
  }

  // 每条解析成功的入站都记一行；环形超了丢最老的。
  add(entry: InboundEntry): void {
    if (!Number.isInteger(entry.id) || entry.text.length === 0) return;
    this.entries.push(entry);
    this.byId.set(entry.id, entry);
    while (this.entries.length > this.cap) {
      const dropped = this.entries.shift();
      if (dropped !== undefined) this.byId.delete(dropped.id);
    }
    this.persist();
  }

  size(): number {
    return this.entries.length;
  }

  contextBefore(target: string, currentIds: (number | null | undefined)[], limits: Policy['context']) {
    const messages: { message_id: string; who: string; speaker: string; text: string; at: string }[] = [];
    const boundary = this.entries.findIndex(row => row.target === target && currentIds.includes(row.id));
    if (!target.startsWith('group:') || boundary < 0) return { messages, omitted: 0 };
    const at = Date.parse(this.entries[boundary].at);
    const eligible = this.entries.slice(0, boundary).filter(row => row.target === target && row.text.trim().length > 0 &&
      Number.isSafeInteger(row.who) && row.who > 0 && Date.parse(row.at) <= at && Date.parse(row.at) >= at - limits.maxAgeMs);
    let room = limits.maxChars - 2; // JSON列表的方括号也计入预算。
    for (const row of eligible.toReversed()) {
      const item = { message_id: String(row.id), who: String(row.who),
        speaker: row.name ? `${row.name}(${row.who})` : String(row.who), text: row.text, at: row.at };
      const size = JSON.stringify(item).length + (messages.length ? 1 : 0);
      if (messages.length >= limits.maxMessages || size > room) break;
      messages.unshift(item);
      room -= size;
    }
    return { messages, omitted: eligible.length - messages.length };
  }

  // 临时文件 + rename：半截写的 JSON 会让下次启动整份环作废。
  private persist(): void {
    const tmp = `${this.path}.tmp`;
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      writeFileSync(tmp, `${JSON.stringify({ entries: this.entries })}\n`, "utf8");
      renameSync(tmp, this.path);
    } catch (error) {
      // 记不住入站，最坏是「引用」判不出来（少一段上下文），绝不值得把发送路径拖崩。
      this.onWarn(`入站环写不进去（${this.path}）：${(error as Error).message}，内存环继续`);
    }
  }
}
