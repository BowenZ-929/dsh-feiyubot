// 「她说过的话」：协议端 ack 里回的 data.message_id 集合。
// 用途只有一个：判断一条 reply 引用的是不是**她自己发过的**消息（引用别人不算定向）。
// 环形上限 500 条，落 .state/own.json，原子写（临时文件 + rename），写盘失败只 warn。
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export const OWN_IDS_RELATIVE_PATH = ".state/own.json";
export const OWN_IDS_CAP = 500;

export type OwnIdsOptions = {
  path: string;
  cap?: number;
  onWarn?: (message: string) => void;
};

export class OwnIds {
  readonly path: string;
  private readonly cap: number;
  private readonly onWarn: (message: string) => void;
  private ids: number[];
  private readonly seen = new Set<number>();

  constructor(options: OwnIdsOptions) {
    this.path = options.path;
    this.cap = options.cap ?? OWN_IDS_CAP;
    this.onWarn = options.onWarn ?? (() => {});
    this.ids = this.load();
    for (const id of this.ids) this.seen.add(id);
  }

  private load(): number[] {
    if (!existsSync(this.path)) return [];
    let raw: unknown;
    try {
      raw = JSON.parse(readFileSync(this.path, "utf8"));
    } catch (error) {
      this.onWarn(`她说过的话读不出来（${this.path}）：${(error as Error).message}，本轮从空集合开始`);
      return [];
    }
    if (typeof raw !== "object" || raw === null) return [];
    const list = (raw as Record<string, unknown>).ids;
    if (!Array.isArray(list)) return [];
    const ids = list.filter((item): item is number => typeof item === "number" && Number.isInteger(item));
    return ids.slice(-this.cap);
  }

  has(id: number): boolean {
    return this.seen.has(id);
  }

  // 记一条「她说过的话」。重复的 id 不占位置；超过上限就丢最老的。
  add(id: number): void {
    if (!Number.isInteger(id) || this.seen.has(id)) return;
    this.seen.add(id);
    this.ids.push(id);
    // 超上限时把最老的挪出去，同时把它从 set 里删掉（否则集合会一直涨）。
    while (this.ids.length > this.cap) {
      const dropped = this.ids.shift();
      if (dropped !== undefined) this.seen.delete(dropped);
    }
    this.persist();
  }

  size(): number {
    return this.ids.length;
  }

  // 临时文件 + rename：半截写的 JSON 会让下次启动整份集合作废。
  private persist(): void {
    const tmp = `${this.path}.tmp`;
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      writeFileSync(tmp, `${JSON.stringify({ ids: this.ids })}\n`, "utf8");
      renameSync(tmp, this.path);
    } catch (error) {
      // 记不住她说过的话，最坏是「引用她」判不出来（少搭一次话），绝不值得把发送路径拖崩。
      this.onWarn(`她说过的话写不进去（${this.path}）：${(error as Error).message}，内存集合继续`);
    }
  }
}
