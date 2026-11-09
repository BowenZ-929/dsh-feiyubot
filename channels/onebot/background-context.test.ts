import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import WebSocket from 'ws';

test('未参与的群聊段成为下一轮旁听背景，当前段原话独立', { timeout: 15000 }, async () => {
  const temp = await mkdtemp(join(tmpdir(), 'qqagent-background-context-'));
  const calls: any[] = [], actions: any[] = [];
  const core = createServer(async (req, res) => {
    let raw = ''; for await (const part of req) raw += part;
    calls.push(JSON.parse(raw));
    res.setHeader('content-type', 'application/json');
    res.end('{"ok":true,"chunks":[]}');
  });
  core.listen(0, '127.0.0.1'); await once(core, 'listening');
  const probe = createServer(); probe.listen(0, '127.0.0.1'); await once(probe, 'listening');
  const port = (probe.address() as { port: number }).port;
  await new Promise<void>(resolve => probe.close(() => resolve()));
  const state = join(temp, 'state');
  const child = spawn(process.execPath, ['server.ts', '--port', String(port), '--core',
    `http://127.0.0.1:${(core.address() as { port: number }).port}`, '--state', state,
    '--home', join(temp, 'home'), '--prefs', join(temp, 'prefs.jsonl')], {
    cwd: fileURLToPath(new URL('.', import.meta.url)),
    env: { ...process.env, QQAGENT_CHANNEL_MODE: 'send' }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let logs = '', socket: WebSocket | undefined;
  child.stdout.on('data', x => { logs += x; }); child.stderr.on('data', x => { logs += x; });
  const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
  const wait = async (fn: () => boolean) => {
    const deadline = Date.now() + 6500;
    while (!fn() && Date.now() < deadline) await sleep(20);
    assert.ok(fn(), logs);
  };
  try {
    await wait(() => logs.includes('反向 WS 已监听'));
    socket = new WebSocket(`ws://127.0.0.1:${port}/ws`); await once(socket, 'open');
    socket.on('message', raw => actions.push(JSON.parse(String(raw))));
    const say = (id: number, text: string, at = false) => socket!.send(JSON.stringify({
      post_type: 'message', message_type: 'group', self_id: 999, group_id: 10000071,
      user_id: 10000420, sender: { user_id: 10000420, card: '测试群友' }, message_id: id,
      message: [...(at ? [{ type: 'at', data: { qq: '999' } }] : []), { type: 'text', data: { text } }],
    }));
    say(901, '聚餐改到河边茶馆，星期日');
    await wait(() => logs.includes('闲聊没抽到'));
    assert.equal(calls.length, 0, '旁听不额外调用核心');
    say(902, '你觉得那个地方怎么样', true);
    await sleep(300);
    say(903, '就是刚才说的聚餐地点');
    await wait(() => calls.length === 1);
    assert.deepEqual(calls[0].segment.map((r: any) => r.text), ['@999你觉得那个地方怎么样', '就是刚才说的聚餐地点']);
    assert.equal(calls[0].background.session_id, 'qq:group:10000071');
    assert.deepEqual(calls[0].background.messages.map((r: any) => [r.message_id, r.who, r.text]),
      [['901', '10000420', '聚餐改到河边茶馆，星期日']]);
    assert.equal(calls[0].background.omitted, 0);
    assert.deepEqual(actions, [], '假核心沉默，不产生QQ发送动作');
  } finally {
    socket?.terminate(); child.kill('SIGTERM'); await once(child, 'exit');
    core.closeAllConnections(); await new Promise<void>(resolve => core.close(() => resolve()));
    await rm(temp, { recursive: true, force: true });
  }
});
