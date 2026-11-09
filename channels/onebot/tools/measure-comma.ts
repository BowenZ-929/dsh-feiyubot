// measure-comma.ts —— 逗号拆分的前后对比测量（可复跑）
//
// 语料优先取生产/演练账里真实的 sent 文本，剔除演练造物（回声、/say、合成群号），
// 不足 pad 条就用固定合成语料补齐，并打印补了多少条。
//
// 用法：
//   node channels/onebot/tools/measure-comma.ts --ledger /tmp/ch-drill/ledger.jsonl
//   node channels/onebot/tools/measure-comma.ts --ledger /tmp/ch-drill/ledger.jsonl --sample 20
import { readFileSync } from "node:fs";
import { splitBubbles } from "../bubbles.ts";
import { loadPolicy } from "../policy.ts";

const arg = (name: string, fallback: string): string => {
  const index = process.argv.indexOf("--" + name);
  const value = index >= 0 ? process.argv[index + 1] : undefined;
  return value && !value.startsWith("--") ? value : fallback;
};

const ledgerPath = arg("ledger", "/tmp/ch-drill/ledger.jsonl");
// 主验收样本 = 500 条（200/20 只作展示；20 条允许 15–35%）。
const sampleSize = Number(arg("sample", "500"));
const padTo = Number(arg("pad-to", String(sampleSize)));

type Row = { kind?: string; text?: unknown; key?: unknown; target?: unknown };

const readRows = (path: string): Row[] => {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch (error) {
    console.error(`读不到账 ${path}：${(error as Error).message}`);
    process.exit(1);
  }
  return raw
    .split("\n")
    .filter((line) => line.trim().length > 0)
    .map((line) => {
      try {
        return JSON.parse(line) as Row;
      } catch {
        return {};
      }
    });
};

// 演练造物：以「回声」开头、/say 写的行、或者 drill 的合成群号（7xxxxxxxx）。
const isSynthetic = (row: Row): boolean => {
  const text = typeof row.text === "string" ? row.text : "";
  if (text.length === 0 || text.startsWith("回声")) return true;
  if (typeof row.key === "string" && row.key.startsWith("say-")) return true;
  const target = typeof row.target === "string" ? row.target : "";
  const match = /^group:(\d{9})$/.exec(target);
  if (match !== null) {
    const id = Number(match[1]);
    if (id >= 700_000_000 && id < 800_000_000) return true;
  }
  return false;
};

const corpus: string[] = [];
for (const row of readRows(ledgerPath)) {
  if (row.kind !== "sent") continue;
  if (typeof row.text !== "string") continue;
  if (isSynthetic(row)) continue;
  if (row.text.trim().length === 0) continue;
  corpus.push(row.text);
}

// 合成语料：把「逗号粘连」的形状固定下来，补齐时用；同一份语料永远一样。
const synthetic = (index: number): string => {
  const heads = [
    "刚才那件事我想了想",
    "今天的安排我改了一下",
    "你昨天说的那个问题",
    "下午的会我先去占个位",
    "外面的雨好像停了",
    "我这会儿在写代码",
    "屋子里有点冷",
    "那家店我上次去过",
  ];
  const tails = [
    "今天天气不错",
    "我们晚点再说",
    "你早点睡",
    "我先去忙了",
    "记得吃晚饭",
    "回头聊",
    "别等我了",
    "我大概八点到",
  ];
  // 语料要够杂，sha256 的命中率才不会被几条固定句式带偏。
  const head = heads[index % heads.length];
  const tail = tails[(index * 3 + 1) % tails.length];
  return `${head}，${tail}（合成 ${index + 1}）`;
};

const realCount = corpus.length;
while (corpus.length < padTo) corpus.push(synthetic(corpus.length - realCount));
const sample = corpus.slice(0, sampleSize);

const policy = loadPolicy();
const run = (ratePct: number) => {
  let bubbles = 0;
  let withComma = 0;
  let commaSplits = 0;
  for (const text of sample) {
    const result = splitBubbles([{ text, gap_before: 0, send_at: 0 }], {
      gapSec: policy.outbound.newlineGapMs / 1000,
      cap: policy.outbound.maxChunks,
      maxChars: policy.outbound.maxCharsPerBubble,
      sentenceMarks: policy.outbound.bubbleSentenceMarks,
      clauseMarks: policy.outbound.bubbleClauseMarks,
      tailParticles: policy.outbound.bubbleTailParticles,
      commaSplitRatePct: ratePct,
      commaSplitMarks: policy.outbound.commaSplitMarks,
    });
    bubbles += result.chunks.length;
    withComma += result.chunks.filter((chunk) => /[，,]/.test(chunk.text)).length;
    if (result.commaSplits.length > 0) commaSplits += 1;
  }
  // 「可拆样本」= 句子里本来就有逗号的那些；没有逗号的样本永远拆不了，
  // 所以拆条率既报「/ 全部样本」，也报「/ 可拆样本」——后者才是 25% 命中率该落在的地方。
  const eligible = sample.filter((text) => /[，,]/.test(text)).length;
  return {
    samples: sample.length,
    eligible,
    bubbles,
    avgBubbles: bubbles / sample.length,
    commaRate: (withComma / bubbles) * 100,
    splitRate: (commaSplits / sample.length) * 100,
    splitRateEligible: eligible === 0 ? 0 : (commaSplits / eligible) * 100,
  };
};

const off = run(0);
const on = run(policy.outbound.commaSplitRatePct);
console.log(
  JSON.stringify(
    {
      ledger: ledgerPath,
      realSamples: realCount,
      padded: Math.max(0, sample.length - realCount),
      samples: sample.length,
      off,
      on,
      commaRateOff: `${off.commaRate.toFixed(1)}%`,
      commaRateOn: `${on.commaRate.toFixed(1)}%`,
      splitRateOn: `${on.splitRate.toFixed(1)}%`,
      splitRateOnEligible: `${on.splitRateEligible.toFixed(1)}%`,
    },
    null,
    2,
  ),
);
