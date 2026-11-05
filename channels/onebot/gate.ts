// 门：模式解析、去重键、以及那本追加写的账。
// 这里不碰 socket、不碰网络：路径、时钟、告警出口都由调用方注入，
// 好让测试用 os.tmpdir() 和假时钟把「重启之后还认不认得出重复」这种事实钉死。
import { createHash } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";

export type Mode = "silent" | "send";
export type Scene = "group" | "private" | "unknown";

export const MODE_ENV = "QQAGENT_CHANNEL_MODE";
export const LEDGER_RELATIVE_PATH = ".state/ledger.jsonl";

export type ModeDecision = { mode: Mode; warn: string | null };

// 默认必须是不说话的那一档：出事时「什么也没发」永远比「发错人」可收拾。
// 认不出的值一律落 silent 并留下原值——fail-open 是这套开关最不该有的失败方式。
export const readMode = (env: Record<string, string | undefined>): ModeDecision => {
  const raw = env[MODE_ENV];
  if (raw === undefined) return { mode: "silent", warn: null };
  if (raw === "silent") return { mode: "silent", warn: null };
  if (raw === "send") return { mode: "send", warn: null };
  return {
    mode: "silent",
    warn: `${MODE_ENV}= 认不出 ${JSON.stringify(raw)}，按 silent 处理（绝不 fail-open）`,
  };
};

// scene 只有三个值，且不许猜：拿不到 id 就是 unknown，猜一个等于替协议端决定发给谁。
// 入参故意收 unknown：解析层类型是窄的，但这道判定在畸形输入下也必须说得出结论。
export const sceneOf = (where: unknown): Scene => {
  if (typeof where !== "object" || where === null) return "unknown";
  const record = where as Record<string, unknown>;
  if (record.scene === "group" && typeof record.groupId === "number" && Number.isFinite(record.groupId)) {
    return "group";
  }
  if (record.scene === "private" && typeof record.userId === "number" && Number.isFinite(record.userId)) {
    return "private";
  }
  return "unknown";
};

// 目标地址只在账和日志里用，不参与 action 构造：账要能独立回答「发给了谁」。
// unknown 就照实写 unknown，不许出现 group:undefined 这种看着像地址的脏东西。
export const targetOf = (where: unknown): string => {
  const scene = sceneOf(where);
  if (scene === "unknown") return "unknown";
  const record = where as Record<string, unknown>;
  return scene === "group" ? `group:${String(record.groupId)}` : `user:${String(record.userId)}`;
};

export type DedupInput = {
  messageId: number | null;
  scene: Scene;
  target: string;
  whoId: number;
  time: number | null;
  text: string;
};

// messageId 最可靠；协议端不给时退化成内容指纹，
// 「同一个人 + 同一场景 + 同一时间 + 同一句话」仍然能判成同一条，重连后照样拦得住。
export const dedupKey = (input: DedupInput): string => {
  if (typeof input.messageId === "number" && Number.isFinite(input.messageId)) {
    return `mid:${input.messageId}`;
  }
  const raw = `${input.scene}|${input.target}|${input.whoId}|${input.time}|${input.text}`;
  return `fp:${createHash("sha1").update(raw, "utf8").digest("hex").slice(0, 16)}`;
};

// empty = 核回得成但一个泡都没有；error = 这一轮压根没走到发（原因写进 note）。
export type LedgerKind = "blocked" | "sent" | "failed" | "uncertain" | "unsent" | "dup" | "empty" | "error";

// claimed 只在内存里存在：已认领、还没落账。它必须能被 firstKind 看见，
// 否则「等核回 / 等 ack」这段窗口里 key 是隐形的，重投就会被领两次。
export type LedgerState = LedgerKind | "claimed";

// 重投分两种，说清是哪一种才算排障有用：账里已有真行＝早走过了；只在飞＝上一条还在处理。
export const dedupNote = (state: LedgerState | null): string =>
  state === null || state === "claimed" ? "同一条正在处理中（在飞窗口重投）" : `已有 kind=${state}`;

// 认领者在异常处的收尾决定：还没落过真行才补一行 error；已经落过就交给那几行说话，
// 否则「按泡 N 行」会被凭空多出来的一行破坏。
export const needsErrorRow = (state: LedgerState | null): boolean => state === null || state === "claimed";

export type LedgerRow = {
  ts: string;
  kind: LedgerKind;
  mode: Mode;
  key: string;
  messageId: number | null;
  scene: Scene;
  target: string;
  index: number | null;
  text: string | null;
  send_at: number | null;
  retcode: number | null;
  status: string | null;
  note: string;
};

// 除 ts 之外的字段：调用方只写它真正知道的东西，其余留空而不是瞎猜。
export type LedgerRowInput = {
  kind: LedgerKind;
  mode: Mode;
  key: string;
  messageId: number | null;
  scene: Scene;
  target: string;
  index?: number | null;
  text?: string | null;
  send_at?: number | null;
  retcode?: number | null;
  status?: string | null;
  note?: string;
};

// 字段顺序写死在这里，账文件才是一份稳定可 grep 的东西。
export const makeRow = (input: LedgerRowInput, ts: string): LedgerRow => ({
  ts,
  kind: input.kind,
  mode: input.mode,
  key: input.key,
  messageId: input.messageId,
  scene: input.scene,
  target: input.target,
  index: input.index ?? null,
  text: input.text ?? null,
  send_at: input.send_at ?? null,
  retcode: input.retcode ?? null,
  status: input.status ?? null,
  note: input.note ?? "",
});

export type LedgerOptions = {
  path: string;
  now?: () => Date;
  onWarn?: (message: string) => void;
};

export class Ledger {
  readonly path: string;
  private readonly now: () => Date;
  private readonly onWarn: (message: string) => void;
  private readonly seen = new Map<string, LedgerKind>();
  // 在飞集合只活在本进程里：重启后自然清空，历史账仍由 load() 拦住。
  private readonly inflight = new Set<string>();

  constructor(options: LedgerOptions) {
    this.path = options.path;
    this.now = options.now ?? (() => new Date());
    this.onWarn = options.onWarn ?? (() => {});
    this.load();
  }

  // 启动时全量读一遍：去重必须在重连和重启之后仍然生效，只放内存里等于白记。
  private load(): void {
    if (!existsSync(this.path)) return;
    let content: string;
    try {
      content = readFileSync(this.path, "utf8");
    } catch (error) {
      // 读不到就不该假装「账是空的所以可以发」，但也不必让通道起不来，交给调用方 warn。
      this.onWarn(`账读不出来（${this.path}）：${(error as Error).message}，本轮去重集合为空`);
      return;
    }
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let value: unknown;
      try {
        value = JSON.parse(trimmed);
      } catch {
        continue; // 半行（上次写到一半崩了）不能让整本账失效
      }
      if (typeof value !== "object" || value === null) continue;
      const record = value as Record<string, unknown>;
      if (typeof record.key !== "string" || typeof record.kind !== "string") continue;
      this.remember(record.key, record.kind as LedgerKind);
    }
  }

  // 真行优先；没有真行但在飞，就报 claimed，谎报成某个已完成的 kind 会误导排障。
  firstKind(key: string): LedgerState | null {
    return this.seen.get(key) ?? (this.inflight.has(key) ? "claimed" : null);
  }

  // 认领必须同步、无 await：两个并发 relay 之间的互斥就靠这一下。
  // 「等 ack 才落账」意味着在飞窗口里账是空的，所以只查账拦不住重投。
  claim(key: string): boolean {
    if (this.seen.has(key) || this.inflight.has(key)) return false;
    this.inflight.add(key);
    return true;
  }

  // 认领者退出必须放掉：异常一次就把 key 永久锁死，这条消息以后再也进不来。
  release(key: string): void {
    this.inflight.delete(key);
  }

  // dup 只是「有人重投过」的旁证，不能盖掉这条消息真正走到哪一步。
  private remember(key: string, kind: LedgerKind): void {
    const existing = this.seen.get(key);
    if (existing === undefined) {
      this.seen.set(key, kind);
      return;
    }
    if (existing === "dup" && kind !== "dup") this.seen.set(key, kind);
  }

  appendNew(input: LedgerRowInput): LedgerRow {
    const row = makeRow(input, this.now().toISOString());
    this.append(row);
    return row;
  }

  append(row: LedgerRow): boolean {
    // 先记内存再落盘：写盘失败也不能让同一条消息在本进程里被处理第二次。
    this.remember(row.key, row.kind);
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      appendFileSync(this.path, `${JSON.stringify(row)}\n`, "utf8");
      return true;
    } catch (error) {
      this.onWarn(`账写不进去（${this.path}）：${(error as Error).message}，通道继续`);
      return false;
    }
  }
}
