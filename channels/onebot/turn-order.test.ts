import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer, type ServerResponse } from "node:http";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import WebSocket from "ws";

const waitFor = async (predicate: () => boolean, label: string) => {
  const end = Date.now() + 12000;
  while (!predicate() && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 20));
  assert.ok(predicate(), label);
};

for (const [failFirst, replyKind, partial] of [[false, undefined, false], [true, undefined, false], [false, "conversation", false], [false, "action_result", false], [false, "conversation", true]] as const) test(`同会话顺序与补充合批，其他会话独立；首轮${failFirst ? "核心失败" : (replyKind ?? "旧协议发送并记忆") + (partial ? "部分已发" : "")}`, { timeout: 25000 }, async () => {
  const temp = await mkdtemp(join(tmpdir(), "qqagent-turn-order-"));
  const calls: { who: { id: number }; segment: { text: string }[]; remembered: boolean }[] = [];
  let first: ServerResponse | undefined, receipt: ServerResponse | undefined;
  let remembered = false;
  let firstReceipt: any;
  const core = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    res.setHeader("content-type", "application/json");
    if (req.url === "/chat/ack") {
      if (body.delivery_id === "first") { firstReceipt = body; receipt = res; return; }
      res.end('{"ok":true}'); return;
    }
    calls.push({ ...body, remembered });
    if (body.who.id === 42 && !first) { first = res; return; }
    res.end(JSON.stringify({ ok: true, chunks: [{ text: "收到补充。", send_at: 0 }], delivery_id: "other" }));
  });
  core.listen(0, "127.0.0.1"); await once(core, "listening");
  const corePort = (core.address() as { port: number }).port;
  const portProbe = createServer(); portProbe.listen(0, "127.0.0.1"); await once(portProbe, "listening");
  const port = (portProbe.address() as { port: number }).port;
  await new Promise<void>(resolve => portProbe.close(() => resolve()));
  const child = spawn(process.execPath, ["server.ts", "--port", String(port), "--core", `http://127.0.0.1:${corePort}`,
    "--state", join(temp, "state"), "--home", join(temp, "home"), "--prefs", join(temp, "prefs.jsonl")], {
    cwd: fileURLToPath(new URL(".", import.meta.url)), env: { ...process.env, QQAGENT_CHANNEL_MODE: "send" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let logs = "", socket: WebSocket | undefined;
  child.stdout.on("data", data => { logs += data; });
  child.stderr.on("data", data => { logs += data; });
  let messageId = 100, sent = 0, firstBubbleSent = false;
  try {
    await waitFor(() => logs.includes("监听") && logs.includes(String(port)), "测试通道监听成功：" + logs);
    socket = new WebSocket(`ws://127.0.0.1:${port}/ws`); await once(socket, "open");
    socket.on("message", data => {
      const frame = JSON.parse(String(data));
      if (frame.action?.startsWith("send_")) {
        sent++;
        if (frame.params.user_id === 42) firstBubbleSent = true;
        socket!.send(JSON.stringify({ status: "ok", retcode: 0, echo: frame.echo, data: { message_id: ++messageId } }));
      }
    });
    const say = (user: number, text: string) => socket!.send(JSON.stringify({ post_type: "message", message_type: "private",
      sub_type: "friend", self_id: 999, user_id: user, sender: { user_id: user, nickname: "测试者" + user },
      time: Math.floor(Date.now() / 1000), message_id: ++messageId, message: [{ type: "text", data: { text } }] }));
    say(42, "我想聊明天的安排");
    await waitFor(() => !!first, "第一轮进入核心");
    if (!partial) {
      say(42, "刚才说错了，是后天");
      say(42, "下午三点以后才有空");
    }
    say(43, "另一个私聊可以正常回");
    await waitFor(() => calls.some(call => call.who.id === 43), "不同会话不被第一轮阻塞");
    assert.equal(calls.filter(call => call.who.id === 42).length, 1, "上一轮生成中，不得并发进入同会话核心");
    if (failFirst) {
      say(42, "晚上也行，先不定下来");
      // 先让第三条入站，再结束失败的第一轮。
      await waitFor(() => logs.includes("晚上也行"), "补充已进入通道");
      first!.writeHead(503); first!.end('{"ok":false,"message":"测试故障"}');
    } else {
      first!.end(JSON.stringify({ ok: true, chunks: [{ text: "你具体什么时候有空？", send_at: 0 },
        ...(partial ? [{text:"剩下的旧稿。",send_at:2,gap_before:2}] : [])], delivery_id: "first", reply_kind: replyKind }));
      if (partial) {
        await waitFor(() => firstBubbleSent, "第一泡已经确认，再补充而不是提前取消全部");
        say(42, "刚才说错了，是后天");
        say(42, "下午三点以后才有空");
      }
      await waitFor(() => !!receipt, "发送或停发后交付实际回执");
      assert.equal(firstReceipt.text, replyKind === 'conversation' && !partial ? '' : '你具体什么时候有空？');
      assert.equal(firstReceipt.stop_reason, replyKind === 'conversation' ? 'superseded' : undefined);
      say(42, "晚上也行，先不定下来");
      await waitFor(() => logs.includes("晚上也行"), "补充已进入通道");
      assert.equal(calls.filter(call => call.who.id === 42).length, 1, "记忆回执未完成，不得开始下一轮");
      remembered = true; receipt!.end('{"ok":true}');
    }
    await waitFor(() => calls.filter(call => call.who.id === 42).length === 2, "回执完成后处理积攒的补充");
    const next = calls.filter(call => call.who.id === 42)[1];
    assert.equal(next.remembered, !failFirst);
    assert.deepEqual(next.segment.map(row => row.who), ["42", "42", "42"], "实际发到核心的每条补充保留发送者编号");
    assert.deepEqual(next.segment.map(row => row.text), ["刚才说错了，是后天", "下午三点以后才有空", "晚上也行，先不定下来"]);
    await waitFor(() => sent === (failFirst || (replyKind === "conversation" && !partial) ? 2 : 3), "成功回合各发送一次，失败回合不发送");
  } finally {
    socket?.terminate();
    first?.destroy(); receipt?.destroy();
    child.kill("SIGTERM"); await once(child, "exit");
    core.closeAllConnections(); await new Promise<void>(resolve => core.close(() => resolve()));
    await rm(temp, { recursive: true, force: true });
  }
});
