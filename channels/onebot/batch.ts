// 合批：把「一段消息」收成一个批次，交给核做一次调用。
// 这里只做时间的账——什么时候开批、deadline 到哪、这批都装了谁。
// 时钟由调用方传入（observe/consume 都收 now），所以 2s / 8s / @ 1s 这些边界能被测试钉成数字。
//
// 按**会话**分批：每个 group:<id> / user:<id> 各有自己的 open、排队和「上一条到达时刻」。
// 会话键直接用 gate.targetOf 那一套（与配额、账里的 target 完全同源），不许自造第二套键。
import type { BatchPolicy, Tier } from "./policy.ts";
import { targetOf } from "./gate.ts";

export type Where = { scene: "group"; groupId: number } | { scene: "private"; userId: number };

// 三个判定（@ 我 / 裸图 / 纯表情）由 parse 说好结论，合批只消费结论，
// 不再回头读一遍消息内容——同一件事只判一次，改判定时只改一处。
export type ObserveInput = {
  senderId: number;
  // 昵称由调用方给（群名片优先）；给空退回 QQ。每行必须有个能读的人名。
  senderName: string;
  text: string;
  mentioned: boolean;
  bareImage: boolean;
  sticker: boolean;
  where: Where;
};

export type BatchMessage = {
  id: number;
  name: string;
  text: string;
  at: number;
  mentioned: boolean;
};

export type BatchSnapshot = {
  batchId: string;
  msgs: BatchMessage[];
  tier: Tier;
  firstAt: number;
  lastAt: number;
  waited_ms: number;
  mentioned: boolean;
  scene: "group" | "private";
  groupId?: number;
  userId?: number;
};

type Open = {
  batchId: string;
  msgs: BatchMessage[];
  tier: Tier;
  firstAt: number;
  lastAt: number;
  lastSpeaker: number;
  deadline: number;
  // 最早一次 @ 她的时刻：@ 的上限绑在"第一次被叫"上，而不是最后一条上，
  // 否则最后补一句 @ 就能把整批再续命一秒。
  atFirst: number | null;
  mentioned: boolean;
  where: Where;
};

export type HubState = {
  open: boolean;
  batchId: string | null;
  tier: Tier | null;
  firstAt: number | null;
  lastAt: number | null;
  deadline: number | null;
  count: number;
  atFirst: number | null;
  mentioned: boolean;
  queued: number;
};

// 会话键：group:<id> / user:<id>。谁要给 hub 报 key，就用这个函数（别自己拼字符串）。
export const conversationKey = (where: Where): string => targetOf(where);

export class BatchHub {
  private readonly policy: BatchPolicy;
  // 每个会话一份状态：A 群聊得再热闹，也不会把 B 群的 deadline 或「安静档」带着走。
  private readonly opens = new Map<string, Open>();
  // 私聊排队也按会话：同一个人的上一条还没取走时先在这里排，绝不合并。
  private readonly queues = new Map<string, BatchSnapshot[]>();
  // 上一条消息的到达时刻也按会话分开（跨批次保留）：安静开批与活跃开批就靠它区分。
  private readonly lastArrivals = new Map<string, number>();
  // batchId 仍是全局递增（b1,b2,…）：切会话不等于切编号，账里只有一个序号空间。
  private seq = 0;

  constructor(policy: BatchPolicy) {
    this.policy = policy;
  }

  observe(input: ObserveInput, now: number): void {
    const key = conversationKey(input.where);
    const msg: BatchMessage = {
      id: input.senderId,
      name: input.senderName.trim() === "" ? String(input.senderId) : input.senderName,
      text: input.text,
      at: now,
      mentioned: input.mentioned,
    };

    if (input.where.scene === "private") {
      this.observePrivate(key, input, msg, now);
      this.lastArrivals.set(key, now);
      return;
    }

    const open = this.opens.get(key) ?? null;
    // 边界判据是「严格晚于」：now == deadline 那一刻仍算活着（还能续），晚 1ms 就只能并入、不许续。
    if (open !== null && now > open.deadline) {
      // 已到期：这条仍然算进这一批（同一段话不该被劈成两批），但 deadline 一个字节都不动。
      // 这是合批与固定窗口的分界线——到期还能续命的话，「再等一条」会被一条一条地续下去，窗口就没有边了。
      this.append(open, input, msg, now);
    } else if (open !== null) {
      this.extend(open, input, msg, now);
    } else {
      this.opens.set(key, this.openNew(input, msg, now, this.lastArrivals.get(key) ?? null));
    }
    this.lastArrivals.set(key, now);
  }

  // 到期才取件，而且只取这一个会话的件。到这里 deadline 已经不会再变，consume 是纯粹的动作。
  consume(key: string, now: number): BatchSnapshot | null {
    const queue = this.queues.get(key);
    if (queue !== undefined && queue.length > 0) {
      const queued = queue.shift();
      if (queue.length === 0) this.queues.delete(key);
      return queued ?? null;
    }
    const open = this.opens.get(key);
    if (open === undefined) return null;
    if (now < open.deadline) return null;
    const snapshot = this.snapshotOf(open);
    this.opens.delete(key);
    return snapshot;
  }

  state(key: string): HubState {
    const open = this.opens.get(key) ?? null;
    const queued = (this.queues.get(key) ?? []).length;
    if (open === null) {
      return {
        open: false,
        batchId: null,
        tier: null,
        firstAt: null,
        lastAt: null,
        deadline: null,
        count: 0,
        atFirst: null,
        mentioned: false,
        queued,
      };
    }
    return {
      open: true,
      batchId: open.batchId,
      tier: open.tier,
      firstAt: open.firstAt,
      lastAt: open.lastAt,
      deadline: open.deadline,
      count: open.msgs.length,
      atFirst: open.atFirst,
      mentioned: open.mentioned,
      queued,
    };
  }

  // 接线层排定时器用：现在有 open 或排队快照的会话键。消费之后要按新 deadline 重排，就靠它。
  openKeys(): string[] {
    const keys = new Set<string>(this.opens.keys());
    for (const [key, queue] of this.queues) {
      if (queue.length > 0) keys.add(key);
    }
    return [...keys];
  }

  private observePrivate(key: string, input: ObserveInput, msg: BatchMessage, now: number): void {
    // 私聊没有"等一会儿看看还有没有下一句"的余地：对面就她一个人，多等一秒只是慢一秒。
    const open = this.opens.get(key) ?? null;
    if (open !== null) {
      const queue = this.queues.get(key) ?? [];
      queue.push(this.snapshotOf(open));
      this.queues.set(key, queue);
    }
    this.opens.set(key, {
      batchId: this.nextId(),
      msgs: [msg],
      tier: "quietOpen",
      firstAt: now,
      lastAt: now,
      lastSpeaker: input.senderId,
      // privateCapMs 是"私聊最多等多久"：0 延迟下取 min 后必然等于 now。
      // 留着这一项不是为了用，而是让系数表在这一档上也有出处，而不是凭空少一个键。
      deadline: Math.min(now, now + this.policy.privateCapMs),
      atFirst: input.mentioned ? now : null,
      mentioned: input.mentioned,
      where: input.where,
    });
  }

  private openNew(input: ObserveInput, msg: BatchMessage, now: number, prevArrival: number | null): Open {
    // 安静久了只等 2s（她可以早点接话）；同一个会话里正热闹就等 5s（把这一段说完再回）。
    const tier: Tier = prevArrival === null || now - prevArrival > this.policy.quietGapMs ? "quietOpen" : "activeOpen";
    return {
      batchId: this.nextId(),
      msgs: [msg],
      tier,
      firstAt: now,
      lastAt: now,
      lastSpeaker: input.senderId,
      deadline: this.capped(now + this.policy.tiersMs[tier], input, now, now, input.mentioned ? now : null),
      atFirst: input.mentioned ? now : null,
      mentioned: input.mentioned,
      where: input.where,
    };
  }

  private append(open: Open, input: ObserveInput, msg: BatchMessage, now: number): void {
    open.msgs.push(msg);
    open.lastAt = now;
    open.lastSpeaker = input.senderId;
    if (input.mentioned) {
      open.mentioned = true;
      open.atFirst = open.atFirst === null ? now : Math.min(open.atFirst, now);
    }
    // tier 说明的是"上一次 deadline 是怎么算出来的"，deadline 冻着的时候 tier 也跟着冻着。
  }

  private extend(open: Open, input: ObserveInput, msg: BatchMessage, now: number): void {
    const tier: Tier = input.senderId === open.lastSpeaker ? "sameSpeaker" : "switchSpeaker";
    open.msgs.push(msg);
    open.lastAt = now;
    open.lastSpeaker = input.senderId;
    if (input.mentioned) {
      open.mentioned = true;
      open.atFirst = open.atFirst === null ? now : Math.min(open.atFirst, now);
    }
    open.tier = tier;
    // 整段重算而不是在原值上取 max：换人（0.5s）本来就该比同人（2s）更早到点，
    // 提前和推后在同一个 min 里表达，才不会被"只许延后"的写法吃掉换人这一档。
    open.deadline = this.capped(now + this.policy.tiersMs[tier], input, now, open.firstAt, open.atFirst);
  }

  // 裸图想多等 4s、纯表情只想赶紧收，但两者都逃不出硬顶与 @ 上限：
  // 系数之间不许有谁绕过硬顶，否则连着刷图的一段能把一批拖到天荒地老。
  private capped(base: number, input: ObserveInput, now: number, firstAt: number, atFirst: number | null): number {
    let deadline = base;
    if (input.bareImage) deadline = Math.max(deadline, now + this.policy.bareImageMs);
    if (input.sticker) deadline = Math.min(deadline, now + this.policy.stickerMs);
    deadline = Math.min(deadline, firstAt + this.policy.hardCapMs);
    if (atFirst !== null) deadline = Math.min(deadline, atFirst + this.policy.mentionCapMs);
    return deadline;
  }

  private nextId(): string {
    this.seq += 1;
    return `b${this.seq}`;
  }

  private snapshotOf(open: Open): BatchSnapshot {
    const snapshot: BatchSnapshot = {
      batchId: open.batchId,
      msgs: open.msgs.map((msg) => ({ ...msg })),
      tier: open.tier,
      firstAt: open.firstAt,
      lastAt: open.lastAt,
      waited_ms: open.lastAt - open.firstAt,
      mentioned: open.mentioned,
      scene: open.where.scene,
    };
    if (open.where.scene === "group") snapshot.groupId = open.where.groupId;
    else snapshot.userId = open.where.userId;
    return snapshot;
  }
}
