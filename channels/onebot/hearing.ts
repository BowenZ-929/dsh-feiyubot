// NapCat 官方 get_record 负责解码，耳的本地工具负责识别；不读取消息里的本地路径。
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Attachment } from "./parse.ts";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const cache = new Map<string, Record<string, unknown>>();
type ReadRecord = (file: string, timeout: number) => Promise<Record<string, unknown> | null>;

export async function hearAttachments(items: Attachment[], home: string, readRecord: ReadRecord): Promise<void> {
  const audios = items.filter((item) => item.kind === "audio");
  if (!audios.length) return;
  let config;
  try {
    const live = join(home, "ear/asr.json");
    config = JSON.parse(readFileSync(existsSync(live) ? live : join(ROOT, "plugin/organs/ear/data/asr.json"), "utf8"));
  } catch {
    for (const item of audios) item.transcription = { status: "unavailable", reason: "config_error" };
    return;
  }
  const python = resolve(ROOT, config.python), model = resolve(ROOT, config.model);
  const soundModel = config.sound_model ? resolve(ROOT, config.sound_model) : "";
  const unavailable = !config.enabled ? "disabled" : !existsSync(python) || !existsSync(join(model, "model.bin")) ? "model_missing" : "";
  const deadline = Date.now() + config.budget_seconds * 1000;
  for (const [index, item] of audios.entries()) {
    if (unavailable) { item.transcription = { status: "unavailable", reason: unavailable }; continue; }
    if (index >= config.max_audios || Date.now() >= deadline) {
      item.transcription = { status: "skipped", reason: "round_budget" }; continue;
    }
    if (!item.file) { item.transcription = { status: "unavailable", reason: "missing_record_id" }; continue; }
    try {
      // 只接受协议端 action 回包的音频数据，不下载任意入站 URL，也不读入站 file 路径。
      const data = await readRecord(item.file, Math.min(10000, deadline - Date.now()));
      const b64 = data?.base64;
      if (typeof b64 !== "string" || !b64) {
        item.transcription = { status: "failed", reason: "record_unavailable" }; continue;
      }
      if (b64.length > Math.ceil(config.max_audio_bytes / 3) * 4) {
        item.transcription = { status: "skipped", reason: "audio_too_large" }; continue;
      }
      const key = new Date().toISOString().slice(0, 10) + JSON.stringify([model, config.language, soundModel]) + createHash("sha256").update(b64).digest("hex");
      if (cache.has(key)) { item.transcription = { ...cache.get(key), cached: true }; continue; }
      const timeout = Math.min(config.timeout_seconds * 1000, deadline - Date.now());
      if (timeout <= 0) { item.transcription = { status: "skipped", reason: "round_budget" }; continue; }
      const output = await new Promise<string>((resolveOutput, reject) => {
        const child = execFile(python, [join(ROOT, "tools/transcribe.py"), "--stdin", "--model", model,
          "--language", config.language, ...(soundModel ? ["--sound-model", soundModel] : [])], { timeout, maxBuffer: 1024 * 1024, encoding: "utf8" },
          (error, stdout) => error ? reject(error) : resolveOutput(stdout));
        child.stdin?.on("error", () => {}); // 子进程错误由上面的退出回调报告。
        child.stdin?.end(b64);
      });
      const result = JSON.parse(output);
      item.transcription = result;
      cache.set(key, result);
      if (cache.size > 256) cache.delete(cache.keys().next().value!);
    } catch (error) {
      item.transcription = { status: "failed", reason: (error as { killed?: boolean }).killed ? "timeout" : "transcription_failed" };
    }
  }
}
