// 每群 1 小时软配额：滑窗数次数，@ 可以突破。
// 软的意思是"到点了就不搭话，但被点名照样回"——把人叫住了还不吭声才是真故障。
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export const QUOTA_RELATIVE_PATH = ".state/quota.json";
// 只有群记冷却；私聊按设计不限次，前缀是唯一能把两者分开的东西（与 gate.targetOf 同一套写法）。
export const GROUP_PREFIX = "group:";

export type QuotaLedgerPolicy = {
  quota: { windowMs: number; perGroupMax: number; mentionCanExceed: boolean };
};

export type ChargeResult = { ok: boolean; used: number; overridden: boolean };

type QuotaState = { groups: Record<string, number[]> };

export type QuotaOptions = {
  path: string;
  policy: QuotaLedgerPolicy;
  onWarn?: (message: string) => void;
};

const emptyState = (): QuotaState => ({ groups: {} });

export class Quota {
  readonly path: string;
  private readonly policy: QuotaLedgerPolicy;
  private readonly onWarn: (message: string) => void;
  private readonly state: QuotaState;

  constructor(options: QuotaOptions) {
    this.path = options.path;
    this.policy = options.policy;
    this.onWarn = options.onWarn ?? (() => {});
    this.state = this.load();
  }

  // 重启后必须接着数：冷却按小时算，进程重开一次就清零等于没有冷却。
  private load(): QuotaState {
    if (!existsSync(this.path)) return emptyState();
    let raw: unknown;
    try {
      raw = JSON.parse(readFileSync(this.path, "utf8"));
    } catch (error) {
      this.onWarn(`配额状态读不出来（${this.path}）：${(error as Error).message}，本轮从零开始数`);
      return emptyState();
    }
    if (typeof raw !== "object" || raw === null) return emptyState();
    const groups = (raw as Record<string, unknown>).groups;
    if (typeof groups !== "object" || groups === null) return emptyState();
    const state = emptyState();
    for (const [key, value] of Object.entries(groups as Record<string, unknown>)) {
      if (!Array.isArray(value)) continue;
      const stamps = value.filter((item): item is number => typeof item === "number" && Number.isFinite(item));
      if (stamps.length > 0) state.groups[key] = stamps;
    }
    return state;
  }

  // 临时文件 + rename：半截写的 JSON 会让下次启动整本账作废，
  // 而 rename 在同一目录里是原子的，读到的要么是旧状态要么是新状态。
  private persist(): void {
    const tmp = `${this.path}.tmp`;
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      writeFileSync(tmp, `${JSON.stringify(this.state)}\n`, "utf8");
      renameSync(tmp, this.path);
    } catch (error) {
      // 配额是软约束：丢了最坏是这个小时多说两句，绝不值得把发送路径拖崩。
      this.onWarn(`配额状态写不进去（${this.path}）：${(error as Error).message}，内存计数继续`);
    }
  }

  // used 一律报"窗口里现在剩几次"（含刚记下的这次）：拒绝时它就是卡住的原因，
  // 通过时它就是"还剩几次"的倒数起点，两种读法都不需要调用方再推一遍。
  charge(key: string, now: number, mentioned: boolean): ChargeResult {
    if (!key.startsWith(GROUP_PREFIX)) return { ok: true, used: 0, overridden: false };
    const { windowMs, perGroupMax, mentionCanExceed } = this.policy.quota;
    const fresh = (this.state.groups[key] ?? []).filter((ts) => now - ts < windowMs);
    if (fresh.length >= perGroupMax) {
      if (mentioned && mentionCanExceed) {
        fresh.push(now);
        this.state.groups[key] = fresh;
        this.persist();
        return { ok: true, used: fresh.length, overridden: true };
      }
      // 拒绝也要回写：剪掉的过期时间戳不落盘，文件会一直挂着早就没用的记录。
      this.state.groups[key] = fresh;
      this.persist();
      return { ok: false, used: fresh.length, overridden: false };
    }
    fresh.push(now);
    this.state.groups[key] = fresh;
    this.persist();
    return { ok: true, used: fresh.length, overridden: false };
  }

  // 确认「她没出手」之后把刚记下的那一次退回来：扣了却没回，等于白占这个群一次额度。
  // 退的是窗口内**最新的一条**（charge 只会 push 递增的 now，但状态文件可能被手改过，取 max 更稳）。
  // 没得退就什么都不做：不抛、不造负数、不留 -1 这种脏东西。
  refund(key: string, now: number): void {
    if (!key.startsWith(GROUP_PREFIX)) return;
    const { windowMs } = this.policy.quota;
    const fresh = (this.state.groups[key] ?? []).filter((ts) => now - ts < windowMs);
    if (fresh.length === 0) return;
    const latest = Math.max(...fresh);
    const rest = fresh.slice();
    rest.splice(rest.indexOf(latest), 1);
    if (rest.length === 0) delete this.state.groups[key];
    else this.state.groups[key] = rest;
    this.persist();
  }
}
