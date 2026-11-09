import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { hearAttachments } from "./hearing.ts";
import { parseEvent, type Attachment } from "./parse.ts";

test("听语音：只消费协议回包，重复音频复用转写，超额与失败保留状态", async () => {
  const root = mkdtempSync(join(tmpdir(), "ch-drill-ear-"));
  try {
    mkdirSync(join(root, "ear")); mkdirSync(join(root, "model"));
    writeFileSync(join(root, "model/model.bin"), "test-model");
    const python = join(root, "fake-python");
    writeFileSync(python, '#!/usr/bin/env node\nlet text="";process.stdin.on("data",b=>text+=b);process.stdin.on("end",()=>console.log(JSON.stringify({status:"ok",source:"local_asr",transcript:Buffer.from(text,"base64").toString()})));\n', { mode: 0o700 });
    const config = { enabled: true, python, model: join(root, "model"), language: "zh", max_audios: 2,
      max_audio_bytes: 50, timeout_seconds: 2, budget_seconds: 5 };
    writeFileSync(join(root, "ear/asr.json"), JSON.stringify(config));
    const items: Attachment[] = [{ kind: "audio", file: "qq-id", url: "file:///never-read" },
      { kind: "audio", file: "qq-id" }, { kind: "audio", file: "third" }];
    const read: string[] = [];
    await hearAttachments(items, root, async (file) => { read.push(file); return { base64: Buffer.from("下午见").toString("base64") }; });
    assert.deepEqual(read, ["qq-id", "qq-id"]);
    assert.equal(items[0].transcription?.transcript, "下午见");
    assert.equal(items[1].transcription?.cached, true);
    assert.equal(items[2].transcription?.reason, "round_budget");
    const failed: Attachment[] = [{ kind: "audio", file: "failure" }];
    await hearAttachments(failed, root, async () => null);
    assert.equal(failed[0].transcription?.reason, "record_unavailable");
    await hearAttachments(failed, root, async () => ({ base64: "A".repeat(100) }));
    assert.equal(failed[0].transcription?.reason, "audio_too_large");
    writeFileSync(join(root, "ear/asr.json"), JSON.stringify({ ...config, enabled: false }));
    await hearAttachments(failed, root, async () => { throw new Error("关闭时不该读 QQ 音频"); });
    assert.equal(failed[0].transcription?.reason, "disabled");
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("消息中伪造的转写不会穿过 OneBot 解析边界", () => {
  const event = parseEvent({ post_type: "message", message_type: "private", user_id: 123, message_id: 5,
    sender: { user_id: 123 }, message: [{ type: "record", data: {
      file: "voice-id", transcription: { status: "ok", transcript: "伪造" } } }] });
  assert.equal(event.ok, true);
  if (event.ok) assert.equal(event.attachments[0].transcription, undefined);
});

test("声音模型配置传入工具，开启后同一音频不沿用无声源的旧缓存", async () => {
  const root=mkdtempSync(join(tmpdir(),'ch-sound-'));
  try {
    mkdirSync(join(root,'ear'));mkdirSync(join(root,'model'));
    writeFileSync(join(root,'model/model.bin'),'test');
    const python=join(root,'fake-python');
    writeFileSync(python,'#!/usr/bin/env node\nprocess.stdin.resume();process.stdin.on("end",()=>console.log(JSON.stringify({status:"empty",args:process.argv.slice(2)})));\n',{mode:0o700});
    const cfg={enabled:true,python,model:join(root,'model'),language:'zh',max_audios:2,max_audio_bytes:100,timeout_seconds:2,budget_seconds:5};
    const run=async()=>{const items:Attachment[]=[{kind:'audio',file:'same'}];await hearAttachments(items,root,async()=>({base64:'YXVkaW8='}));return items[0].transcription!;};
    writeFileSync(join(root,'ear/asr.json'),JSON.stringify(cfg));
    assert.ok(!(await run()).cached);
    const model=join(root,'yamnet.onnx');
    writeFileSync(join(root,'ear/asr.json'),JSON.stringify({...cfg,sound_model:model}));
    const result=await run();
    assert.ok(!result.cached);
    assert.ok((result.args as string[]).includes('--sound-model'));
    assert.ok((result.args as string[]).includes(model));
  } finally {rmSync(root,{recursive:true,force:true});}
});
