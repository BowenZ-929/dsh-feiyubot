// 合批：把「一段消息」收成一个批次，交给核做一次调用。
// 这里只做时间的账——什么时候开批、deadline 到哪、这批都装了谁。
// 时钟由调用方传入（observe/consume 都收 now），所以 2s / 8s / @ 1s 这些边界能被测试钉成数字。
import type { BatchPolicy, Tier } from "./policy.ts";

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

export class BatchHub {
  private readonly policy: BatchPolicy;
  private open: Open | null = null;
  // 私聊每来一条就自成一批；上一条要是还没被取走，先在这里排队，绝不合并。
  private readonly queue: BatchSnapshot[] = [];
  private seq = 0;
  // 上一条消息的到达时刻（跨批次保留）：安静开批与活跃开批就靠它区分。
  private lastArrivalAt: number | null = null;

  constructor(policy: BatchPolicy) {
    this.policy = policy;
  }

  observe(input: ObserveInput, now: number): void {
    const msg: BatchMessage = {
      id: input.senderId,
      name: input.senderName.trim() === "" ? String(input.senderId) : input.senderName,
      text: input.text,
      at: now,
      mentioned: input.mentioned,
    };

    if (input.where.scene === "private") {
      this.observePrivate(input, msg, now);
      this.lastArrivalAt = now;
      return;
    }

    const prevArrival = this.lastArrivalAt;
    if (this.open !== null && now > this.open.deadline) {
      // 到期 = now > deadline：now 正好等于 deadline 的那一刻还算活着，可以续。
      // 定时器就是在 deadline 那一刻出手的，而恰好那一刻进来的话只要能接上就不该被劈到下一段：
      // 间隔 2s + quietOpen 2000 的算术是 0→2000、2000→4000、4000→6000，三条一批。
      // 撞硬顶也续不出圈：now == firstAt+hardCap 时重算得到 min(now+tiers, firstAt+hardCap) = firstAt+hardCap，出手不推后。
      // 而已到期的那条仍然算进这一批（同一段话不该被劈成两批），只是 deadline 一个字节都不动——
      // 否则「再等一条」会被一条一条地续下去，窗口就没有边了。
      this.append(this.open, input, msg, now);
    } else if (this.open !== null) {
      this.extend(this.open, input, msg, now);
    } else {
      this.open = this.openNew(input, msg, now, prevArrival);
    }
    this.lastArrivalAt = now;
  }

  // 到期才取件。到这里 deadline 已经不会再变，consume 是纯粹的动作，不参与任何时长计算。
  consume(now: number): BatchSnapshot | null {
    const queued = this.queue.shift();
    if (queued !== undefined) return queued;
    if (this.open === null) return null;
    if (now < this.open.deadline) return null;
    const snapshot = this.snapshotOf(this.open);
    this.open = null;
    return snapshot;
  }

  state(): HubState {
    const open = this.open;
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
        queued: this.queue.length,
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
      queued: this.queue.length,
    };
  }

  private observePrivate(input: ObserveInput, msg: BatchMessage, now: number): void {
    // 私聊没有"等一会儿看看还有没有下一句"的余地：对面就她一个人，多等一秒只是慢一秒。
    if (this.open !== null) {
      this.queue.push(this.snapshotOf(this.open));
      this.open = null;
    }
    this.open = {
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
    };
  }

  private openNew(input: ObserveInput, msg: BatchMessage, now: number, prevArrival: number | null): Open {
    // 安静久了只等 2s（她可以早点接话）；群里正热闹就等 5s（把这一段说完再回）。
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
