// 决策账：每批恰好一行，人话给人看、结构化给面板指认。
// 没回也必须落一行——"她在听"这件事唯一能拿出来的证据就是这个文件。
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

export const DECISIONS_RELATIVE_PATH = ".state/decisions.jsonl";

export type DecisionPolicy = { decide: { reasonCodes: string[] } };

export type BatchSnapshot = {
  id: string;
  msgs: number;
  waited_ms: number;
  tier: string;
  at: string;
};

// code 是这道门"如果拦下会给出哪个 reason_code"：门名给人认，code 给账认，
// 两样都要，否则面板只能说"某道门拦了"却说不出按哪条理由。
export type GateInput = { name: string; code: string; hit: boolean };
export type GateRecord = { name: string; hit: boolean };

export type DecisionInput = {
  scene: "group" | "private";
  mentioned: boolean;
  // target 用 gate.targetOf 那套写法（group:<id> / user:<id>）：面板要能直接回答「发给谁」。
  target: string;
  // reason 是核给的自由文本（例如主动沉默的理由），没有就 null。
  // 它由 machine.reason 单独承载——why 只描述门，绝不许再把理由塞进门名里。
  reason?: string | null;
  // source 缺省必须是 inbound：既有的入站调用点一行不改也能继续工作。
  // 主动开口（POST /say）必须显式写 proactive，别让两条路在账上长得一样。
  source?: "inbound" | "proactive";
  batch: BatchSnapshot;
  gates: GateInput[];
};

export type DecisionMachine = {
  batch: BatchSnapshot;
  gates: GateRecord[];
  decided: "sent" | "silent";
  why: string;
  reason_code: string;
  scene: "group" | "private";
  target: string;
  reason: string | null;
  source: "inbound" | "proactive";
};

export type DecisionRow = {
  ts: string;
  kind: "decision";
  scene: "group" | "private";
  human: string;
  machine: DecisionMachine;
};

// 文案是契约的一部分（面板要能按句搜），所以固定在这里而不是从 policy 读。
// 能替代码做决定的是系数，不是措辞。
const HUMAN_SILENT: Record<string, string> = {
  silent_mode: "没回：门关着（silent）",
  quota_exceeded: "没回：这个群这个小时说得够多了",
  core_empty: "没回：她这轮不想说",
  core_error: "没回：核出了问题",
  // ack 超时 / 连接断开时我们并不知道对方收没收到：措辞必须是「没确认」，不是「没发」，更不是「核坏了」。
  // 与「远端不确认就改判 uncertain、永不自动重发」同源。
  outbound_failed: "没回：发出去了但没确认（协议端没回执）",
  no_mention: "没回：没人叫我",
  replaced_too_long: "回了：本来太长，换成一句短的",
};

export const humanOf = (reasonCode: string, mentioned: boolean): string => {
  if (reasonCode === "sent") return mentioned ? "回了：被 @" : "回了：接话";
  const text = HUMAN_SILENT[reasonCode];
  // 认不出的 code 不许糊成一句笼统的话：那会让"没回"的账看起来像"回了"。
  if (text === undefined) throw new RangeError(`没有 reason_code=${JSON.stringify(reasonCode)} 对应的人话`);
  return text;
};

// replaced_too_long 也是发出去了（短信一条），decided 必须是 sent，
// 否则面板统计"回了多少条"会少算这批。
export const decidedOf = (reasonCode: string): "sent" | "silent" =>
  reasonCode === "sent" || reasonCode === "replaced_too_long" ? "sent" : "silent";

const whyOf = (gates: GateRecord[], blockingIndex: number, reasonCode: string): string => {
  if (reasonCode === "replaced_too_long") return "三道上限超了，整批换成一句短的";
  if (blockingIndex >= 0) return `第 ${blockingIndex + 1} 道门 ${gates[blockingIndex].name} 拦下了`;
  if (gates.length === 0) return "没有可判的门，照发";
  return `门全过（${gates.length} 道），照发`;
};

// 纯函数：同一个输入永远同一行，方便单测把"每批一行"钉死。
export const makeDecision = (
  input: DecisionInput,
  ts: string,
  reasonCodes: string[],
): DecisionRow => {
  if (typeof input.batch.id !== "string" || input.batch.id.length === 0) {
    throw new RangeError("decision 行必须有 batch.id，否则面板没法把账和批次对起来");
  }
  // 这里的 msgs 是条数，不是 SegmentBatch 里那份消息数组。名字撞了，
  // 所以宁可在这里当场炸掉：写进账的数组会让面板把条数算成 NaN，而且再也查不出来源。
  if (typeof input.batch.msgs !== "number" || !Number.isFinite(input.batch.msgs)) {
    throw new RangeError(`machine.batch.msgs 必须是条数（数字），拿到的是 ${typeof input.batch.msgs}`);
  }
  if (typeof input.batch.waited_ms !== "number" || !Number.isFinite(input.batch.waited_ms)) {
    throw new RangeError("machine.batch.waited_ms 必须是数字");
  }
  // 账要能独立回答「发给谁」：没有 target 的行只能靠猜，那就不是账了。
  if (typeof input.target !== "string" || input.target.trim().length === 0) {
    throw new RangeError("decision 行必须有 target（group:<id> / user:<id>）");
  }
  // 自由文本只收非空字符串：对象/数组/空串在账里都读不出来，一律记 null。
  const reason =
    typeof input.reason === "string" && input.reason.trim().length > 0 ? input.reason.trim() : null;
  // 来源只有两种（入站 / 主动开口）。认不出的值当场炸，别让面板收到第三种。
  const source = input.source ?? "inbound";
  if (source !== "inbound" && source !== "proactive") {
    throw new RangeError(`machine.source 只认 inbound / proactive，拿到 ${JSON.stringify(input.source)}`);
  }
  const gates: GateRecord[] = input.gates.map((gate) => ({ name: gate.name, hit: gate.hit === true }));
  const blockingIndex = input.gates.findIndex((gate) => gate.hit === true);
  const reasonCode = blockingIndex >= 0 ? input.gates[blockingIndex].code : "sent";
  // 只允许 policy 列过的那几个值：多出来的第四个值会让面板的着色表直接失配。
  if (!reasonCodes.includes(reasonCode)) {
    throw new RangeError(
      `reason_code=${JSON.stringify(reasonCode)} 不在 policy.decide.reasonCodes（${reasonCodes.join(", ")}）里`,
    );
  }
  for (const gate of input.gates) {
    if (!reasonCodes.includes(gate.code)) {
      throw new RangeError(`门 ${gate.name} 的 code=${JSON.stringify(gate.code)} 不在 policy.decide.reasonCodes 里`);
    }
  }
  return {
    ts,
    kind: "decision",
    scene: input.scene,
    human: humanOf(reasonCode, input.mentioned),
    machine: {
      batch: {
        id: input.batch.id,
        msgs: input.batch.msgs,
        waited_ms: input.batch.waited_ms,
        tier: input.batch.tier,
        at: input.batch.at,
      },
      gates,
      decided: decidedOf(reasonCode),
      why: whyOf(gates, blockingIndex, reasonCode),
      reason_code: reasonCode,
      scene: input.scene,
      target: input.target,
      reason,
      source,
    },
  };
};

export type DecisionLogOptions = {
  path: string;
  policy: DecisionPolicy;
  now?: () => Date;
  onWarn?: (message: string) => void;
};

// 一 append 一行：调用方一批只该调一次，账里的行数就是批数。
export class DecisionLog {
  readonly path: string;
  private readonly policy: DecisionPolicy;
  private readonly now: () => Date;
  private readonly onWarn: (message: string) => void;

  constructor(options: DecisionLogOptions) {
    this.path = options.path;
    this.policy = options.policy;
    this.now = options.now ?? (() => new Date());
    this.onWarn = options.onWarn ?? (() => {});
  }

  append(input: DecisionInput): DecisionRow {
    const row = makeDecision(input, this.now().toISOString(), this.policy.decide.reasonCodes);
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      appendFileSync(this.path, `${JSON.stringify(row)}\n`, "utf8");
    } catch (error) {
      // 账写不进去不等于可以不发：但也不能假装没发生，留 warn 让运维看得见。
      this.onWarn(`决策账写不进去（${this.path}）：${(error as Error).message}，本轮决定照常执行`);
    }
    return row;
  }
}
