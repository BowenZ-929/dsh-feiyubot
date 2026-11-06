// 群段序号：该群每收到一段就 +1，落 .state/segments.json（形如 {"counters":{"group:<id>": n}}）。
// 闲聊抽样就靠它（ordinal % chitchatSampleMod == 0），所以它必须**跨重启连续**、且跟着 --state 走。
// 私聊不计数：这套只服务群聊抽样。
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export const SEGMENTS_RELATIVE_PATH = ".state/segments.json";

export type SegmentOptions = {
  path: string;
  onWarn?: (message: string) => void;
};

export class SegmentCounter {
  readonly path: string;
  private readonly onWarn: (message: string) => void;
  private readonly counters: Map<string, number>;

  constructor(options: SegmentOptions) {
    this.path = options.path;
    this.onWarn = options.onWarn ?? (() => {});
    this.counters = this.load();
  }

  private load(): Map<string, number> {
    const counters = new Map<string, number>();
    if (!existsSync(this.path)) return counters;
    let raw: unknown;
    try {
      raw = JSON.parse(readFileSync(this.path, "utf8"));
    } catch (error) {
      // 读不出来就从 0 数：最坏是抽样节奏从头开始，绝不能让通道起不来。
      this.onWarn(`段序号读不出来（${this.path}）：${(error as Error).message}，本轮从 0 开始数`);
      return counters;
    }
    if (typeof raw !== "object" || raw === null) return counters;
    const table = (raw as Record<string, unknown>).counters;
    if (typeof table !== "object" || table === null) return counters;
    for (const [key, value] of Object.entries(table as Record<string, unknown>)) {
      if (typeof value === "number" && Number.isInteger(value) && value >= 0) counters.set(key, value);
    }
    return counters;
  }

  // 每收到一段调一次：+1、落盘、返回新序号。写盘失败只 warn，内存计数继续。
  next(key: string): number {
    const value = (this.counters.get(key) ?? 0) + 1;
    this.counters.set(key, value);
    this.persist();
    return value;
  }

  peek(key: string): number {
    return this.counters.get(key) ?? 0;
  }

  // 临时文件 + rename：半截写的 JSON 会让下次启动整份计数作废（抽样节奏会乱）。
  private persist(): void {
    const tmp = `${this.path}.tmp`;
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      const counters: Record<string, number> = {};
      for (const [key, value] of this.counters) counters[key] = value;
      writeFileSync(tmp, `${JSON.stringify({ counters })}\n`, "utf8");
      renameSync(tmp, this.path);
    } catch (error) {
      this.onWarn(`段序号写不进去（${this.path}）：${(error as Error).message}，内存计数继续`);
    }
  }
}
