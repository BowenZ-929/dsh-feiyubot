// 出站：把核给的 chunks 变成按 send_at 排定的发送计划，再严格串行地一泡泡发出去。
// 网络、时钟、等待、账本全部注入——这里只做决定，不碰全局状态，测试才能把时序钉死。
import type { Where } from "./parse.ts";
import type { LedgerRowInput, Mode, Scene } from "./gate.ts";

export type Chunk = { text: string; gap_before: number; send_at: number };

// sendAt 是本批起点（performance.now() 基准）加上 send_at 秒后的绝对目标时刻。
export type PlanEntry = { index: number; text: string; send_at: number; sendAt: number };

// 协议端不回话时最多等这么久：等太久会把后面几泡全拖晚，等太短会把还在路上的应答误判成不确定。
export const ACK_TIMEOUT_MS = 10_000;

// 送核的超时：真模型调用常态就是 3–6s，偶尔更久，5s 会把正常的一轮掐死。
// 一旦被掐死，这条消息就按 error 锁死（重投只会 dup），所以默认必须给足。
export const CORE_TIMEOUT_MS = 60_000;

export type CoreTimeout = { ms: number; warn: string | null };

// CLI 值解析必须显式回落：静默把 --core-timeout 1500 吃掉，排障时会被误导成「配了但没生效」。
// raw 为 null 表示根本没给这个参数，那是常态，不告警。
export const readCoreTimeout = (raw: string | null): CoreTimeout => {
  if (raw === null) return { ms: CORE_TIMEOUT_MS, warn: null };
  const text = raw.trim();
  if (text.length === 0) {
    return { ms: CORE_TIMEOUT_MS, warn: `--core-timeout= 缺值，回落 ${CORE_TIMEOUT_MS}ms` };
  }
  const value = Number(text);
  if (!Number.isFinite(value) || value <= 0) {
    return { ms: CORE_TIMEOUT_MS, warn: `--core-timeout= 认不出 ${JSON.stringify(raw)}，回落 ${CORE_TIMEOUT_MS}ms` };
  }
  return { ms: Math.round(value), warn: null };
};

// silent = 核主动说「这轮不说话」（响应里的 send:false）。它不是故障，也不是「憋回去了」：
// 前者要照实记一行 silent，后者才是 empty。reason 只收一句人话，核没给就是 null。
export type CoreSilence = { reason: string | null };

export type CoreReply =
  | { ok: true; chunks: Chunk[]; silent?: CoreSilence }
  | { ok: false; reason: string };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const asText = (value: unknown): string | undefined =>
  typeof value === "string" ? value : undefined;

const asSeconds = (value: unknown): number =>
  typeof value === "number" && Number.isFinite(value) ? value : 0;

// 核的响应按固定顺序认：chunks 优先于 text，两者都认不出就是本轮不发（绝不猜着发）。
export const parseCoreResponse = (payload: unknown): CoreReply => {
  if (!isRecord(payload)) return { ok: false, reason: "响应不是 JSON 对象" };
  // send:false 优先于 ok：主动沉默是核的意思，不该因为 ok 写没写、写成什么就被判成「响应认不出」。
  if (payload.send === false) {
    const text = asText(payload.reason);
    // reason 只收非空字符串：对象/数组塞进账里，面板与 grep 都读不出来。
    return { ok: true, chunks: [], silent: { reason: text !== undefined && text.trim().length > 0 ? text.trim() : null } };
  }
  if (payload.ok !== true) {
    const detail = asText(payload.error) ?? asText(payload.message);
    return { ok: false, reason: `ok 不是 true${detail ? `（${detail}）` : ""}` };
  }
  if (Array.isArray(payload.chunks)) {
    const chunks: Chunk[] = [];
    for (let i = 0; i < payload.chunks.length; i += 1) {
      const item = payload.chunks[i];
      if (!isRecord(item)) return { ok: false, reason: `chunks[${i}] 不是对象` };
      if (typeof item.text !== "string") return { ok: false, reason: `chunks[${i}].text 不是字符串` };
      chunks.push({
        text: item.text,
        gap_before: asSeconds(item.gap_before),
        send_at: asSeconds(item.send_at),
      });
    }
    return { ok: true, chunks };
  }
  const text = asText(payload.text);
  if (text !== undefined) return { ok: true, chunks: [{ text, gap_before: 0, send_at: 0 }] };
  return { ok: false, reason: "既没有 chunks 数组也没有 text 字符串" };
};

// 空白泡不占位：真发出去只会让协议端报空消息，还白占一个泡序号。
// 序号在建计划时才分配，所以丢掉空泡不会在 echo 里留洞。
export const planChunks = (chunks: Chunk[], origin: number): PlanEntry[] => {
  const plan: PlanEntry[] = [];
  for (const chunk of chunks) {
    if (chunk.text.trim().length === 0) continue;
    const send_at = asSeconds(chunk.send_at);
    plan.push({
      index: plan.length,
      text: chunk.text,
      send_at,
      // 负的 send_at 视为「早就该发了」：不补发、不排队，发的那一刻就是现在。
      sendAt: origin + Math.max(0, send_at) * 1000,
    });
  }
  return plan;
};

export const echoFor = (requestSeq: number, index: number): string => `req-${requestSeq}-${index}`;

export const buildAction = (where: Where, text: string, echo: string): Record<string, unknown> => {
  const message = [{ type: "text", data: { text } }];
  if (where.scene === "group") {
    return { action: "send_group_msg", params: { group_id: where.groupId, message }, echo };
  }
  return { action: "send_private_msg", params: { user_id: where.userId, message }, echo };
};

// messageId 是协议端在 ack 里回的 data.message_id：M7 用它记住「她说过的话」，
// 之后别人引用这条才算「引用她」（引用 id 与她自己发过的 id 比对）。
export type AckOutcome =
  | { kind: "ack"; status: string | null; retcode: number | null; messageId: number | null; note: string }
  | { kind: "timeout" }
  | { kind: "closed" };

export type BatchInput = {
  mode: Mode;
  // 账的固定部分：一批里每一行都带同样的 key / 场景 / 目标。
  batch: { key: string; messageId: number | null; scene: Scene; target: string };
  where: Where;
  plan: PlanEntry[];
  requestSeq: number;
  ledger: { appendNew: (input: LedgerRowInput) => unknown };
  isGateOpen: () => boolean;
  isSocketOpen: () => boolean;
  send: (frame: string) => void;
  waitAck: (echo: string) => Promise<AckOutcome>;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
  log: (tag: string, message: string) => void;
};

export type BatchResult = { sent: number; stopped: string | null };

// 原文进日志要截断：一整段长文会把一行日志撑成两屏，日志就没法扫了。
const clip = (text: string, limit: number): string =>
  text.length > limit ? `${text.slice(0, limit)}…` : text;

type RowPatch = Omit<LedgerRowInput, "mode" | "key" | "messageId" | "scene" | "target">;

export const runBatch = async (input: BatchInput): Promise<BatchResult> => {
  const { batch, plan, mode } = input;
  const row = (patch: RowPatch): void => {
    input.ledger.appendNew({
      mode,
      key: batch.key,
      messageId: batch.messageId,
      scene: batch.scene,
      target: batch.target,
      ...patch,
    });
  };
  const total = plan.length;

  // 门挡住的那一路：计划照算、账照记，只是绝不落到 socket 上。
  if (mode !== "send") {
    const whole = plan.map((entry) => entry.text).join("");
    row({
      kind: "blocked",
      text: whole,
      send_at: null,
      note: `chunks=${total} send_at=[${plan.map((entry) => entry.send_at).join(",")}]`,
    });
    input.log("blocked", `mode=silent，想发但被门挡住 chunks=${total}：${clip(whole, 200)}`);
    return { sent: 0, stopped: "blocked" };
  }

  // 一泡都没发出去时也要把后面那些泡记清楚，否则账上看不出批次被打断了。
  const abandon = (from: number, reason: string): void => {
    for (const entry of plan.slice(from)) {
      row({
        kind: "unsent",
        index: entry.index,
        text: entry.text,
        send_at: entry.send_at,
        note: reason,
      });
    }
    input.log("unsent", `#${from}/${total} 起 ${total - from} 泡未发：${reason}`);
  };

  let sentCount = 0;
  for (let i = 0; i < total; i += 1) {
    const entry = plan[i];
    // 目标时刻已过就立刻发：不补等、也不补发，绝不为追进度连发两次。
    const wait = entry.sendAt - input.now();
    if (wait > 0) await input.sleep(wait);

    // 发之前逐个复查：门可能被关，连接可能在等上一泡 ack 的时候断了。
    const blocked = !input.isGateOpen()
      ? "门已关（mode 已不是 send）"
      : !input.isSocketOpen()
        ? "连接已断"
        : null;
    if (blocked !== null) {
      abandon(i, blocked);
      return { sent: sentCount, stopped: blocked };
    }

    const echo = echoFor(input.requestSeq, entry.index);
    input.log(
      "chunk",
      `#${entry.index}/${total} echo=${echo} text=${JSON.stringify(entry.text).slice(0, 60)}`,
    );
    try {
      input.send(JSON.stringify(buildAction(input.where, entry.text, echo)));
    } catch (error) {
      const reason = `send 抛错：${(error as Error).message}`;
      row({ kind: "failed", index: entry.index, text: entry.text, send_at: entry.send_at, note: reason });
      input.log("fail", `#${entry.index}/${total} ${reason}（echo=${echo}）`);
      abandon(i + 1, reason);
      return { sent: sentCount, stopped: reason };
    }

    // 严格串行：下一泡排在这一次 ack 或超时之后，不许并发抢跑。
    const outcome = await input.waitAck(echo);
    if (outcome.kind === "timeout") {
      const note = `ack 超时（echo=${echo}）`;
      row({ kind: "uncertain", index: entry.index, text: entry.text, send_at: entry.send_at, note });
      input.log("fail", note);
      abandon(i + 1, note);
      return { sent: sentCount, stopped: note };
    }
    if (outcome.kind === "closed") {
      const note = `等 ack 时连接断开（echo=${echo}）`;
      row({ kind: "uncertain", index: entry.index, text: entry.text, send_at: entry.send_at, note });
      input.log("fail", note);
      abandon(i + 1, note);
      return { sent: sentCount, stopped: note };
    }
    if (outcome.retcode !== 0 || outcome.status === "failed") {
      const note = outcome.note || "ack 失败";
      row({
        kind: "failed",
        index: entry.index,
        text: entry.text,
        send_at: entry.send_at,
        retcode: outcome.retcode,
        status: outcome.status,
        note,
      });
      // 不重试、不降级、不换目标：换个目标发出去比不发更难收拾。
      input.log(
        "fail",
        `ack 失败 echo=${echo} status=${String(outcome.status)} retcode=${String(outcome.retcode)} ${note}`,
      );
      abandon(i + 1, `前一泡 ack 失败（echo=${echo}）`);
      return { sent: sentCount, stopped: `ack 失败（echo=${echo}）` };
    }

    row({
      kind: "sent",
      index: entry.index,
      text: entry.text,
      send_at: entry.send_at,
      retcode: outcome.retcode,
      status: outcome.status,
      note: outcome.note,
    });
    sentCount += 1;
  }
  return { sent: sentCount, stopped: null };
};

export type CoreAttempt =
  | { kind: "unreachable"; message: string }
  | { kind: "http"; status: number; body: string }
  | { kind: "unreadable"; reason: string }
  // 超时是独立一种：它和「连不上」混成一个前缀，排障时就分不清该调超时还是查网络。
  | { kind: "timeout"; timeoutMs: number; message: string }
  | { kind: "payload"; payload: unknown };

export type SettleInput = {
  mode: Mode;
  batch: { key: string; messageId: number | null; scene: Scene; target: string };
  where: Where;
  scene: Scene;
  attempt: CoreAttempt;
  origin: number;
  requestSeq: number;
  elapsed: () => string;
  ledger: { appendNew: (input: LedgerRowInput) => unknown };
  isGateOpen: () => boolean;
  isSocketOpen: () => boolean;
  send: (frame: string) => void;
  waitAck: (echo: string) => Promise<AckOutcome>;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
  log: (tag: string, message: string) => void;
};

export type SettleResult = {
  kind: "error" | "empty" | "silent" | "blocked" | "ran";
  note: string;
  plan: PlanEntry[];
  batch: BatchResult | null;
};

// 一轮的收尾：错在哪儿都要在账上留下一行，否则「她没说」和「门拦了」「核坏了」在账上分不出来。
// 行数规则：走到发 → 每泡一行；没走到发 → 恰好一行（error / empty / blocked / dup 由调用方各管各的）。
// error 与 empty 之后同一条消息就被锁死了（重投只会是 dup）——这是人拍的口径：
// 宁可丢一句、也不重发或重写一次脑记忆；这里不许为了「补救」放行重投。
export const settleRound = async (input: SettleInput): Promise<SettleResult> => {
  const { batch, attempt, mode } = input;
  const id = input.requestSeq;
  const row = (patch: RowPatch): void => {
    input.ledger.appendNew({
      mode,
      key: batch.key,
      messageId: batch.messageId,
      scene: batch.scene,
      target: batch.target,
      ...patch,
    });
  };
  const failed = (note: string, logMessage: string): SettleResult => {
    row({ kind: "error", note });
    input.log("error", `req#${id} ${logMessage}（用时 ${input.elapsed()}）`);
    return { kind: "error", note, plan: [], batch: null };
  };

  if (attempt.kind === "unreachable") {
    return failed(`核不可达：${attempt.message}`, `核不可达，本轮不发：${attempt.message}`);
  }
  if (attempt.kind === "timeout") {
    return failed(
      `核超时（${attempt.timeoutMs}ms）：${attempt.message}`,
      `核超时（${attempt.timeoutMs}ms），本轮不发：${attempt.message}`,
    );
  }
  if (attempt.kind === "http") {
    const excerpt = attempt.body.trim().slice(0, 200);
    return failed(`核 HTTP ${attempt.status}：${excerpt}`, `核回 HTTP ${attempt.status}，本轮不发`);
  }
  // scene 认不出就绝不出站：没有目标就只能猜，猜错就是发给不相干的人。核已经照常调过了。
  if (input.scene === "unknown") {
    return failed("scene 认不出（unknown），不发", "scene 认不出（unknown），不发");
  }

  const reply =
    attempt.kind === "unreadable"
      ? { ok: false as const, reason: attempt.reason }
      : parseCoreResponse(attempt.payload);
  if (!reply.ok) {
    return failed(`响应认不出：${reply.reason}`, `核的响应认不出：${reply.reason}，本轮不发`);
  }

  // 核主动说「这轮不说话」：一行 silent，一个 action 都不构造，也不重试。
  // 理由进 note——「她今天不想说」和「她憋回去了」在账上必须一眼分得开。
  if (reply.silent !== undefined) {
    const reason = reply.silent.reason;
    const note = reason === null ? "她这轮不想说（核没给理由）" : `她这轮不想说：${reason}`;
    row({ kind: "silent", note });
    input.log("plan", `req#${id} ${note}`);
    return { kind: "silent", note, plan: [], batch: null };
  }

  // send_at 相对这一批，起点是「算计划的这一刻」，不是事件到达的那一刻。
  const plan = planChunks(reply.chunks, input.origin);
  if (plan.length === 0) {
    // 有 chunks 但全是空白，和「一个泡都没有」是同一件事：她这轮没说话。
    const note = reply.chunks.length === 0 ? "chunks=0（她这轮没说）" : `chunks=${reply.chunks.length} 全是空白泡`;
    row({ kind: "empty", note });
    input.log("plan", `req#${id} ${note}`);
    return { kind: "empty", note, plan, batch: null };
  }
  input.log("plan", `req#${id} chunks=${plan.length} send_at=[${plan.map((entry) => entry.send_at).join(",")}]`);

  const batchResult = await runBatch({
    mode,
    batch,
    where: input.where,
    plan,
    requestSeq: id,
    ledger: input.ledger,
    isGateOpen: input.isGateOpen,
    isSocketOpen: input.isSocketOpen,
    send: input.send,
    waitAck: input.waitAck,
    sleep: input.sleep,
    now: input.now,
    log: input.log,
  });
  return { kind: mode === "send" ? "ran" : "blocked", note: "", plan, batch: batchResult };
};
