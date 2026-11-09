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

test('群聊先@后补充经真实通道只形成一个定向请求与段计数', { timeout: 15000 }, async () => {
  const temp = await mkdtemp(join(tmpdir(), 'qqagent-mention-batch-'));
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
    say(901, '', true);
    await wait(() => logs.includes('key=mid:901 → b1'));
    await sleep(1400);
    assert.equal(calls.length, 0, '只有@时，1秒旧截止不得提前请求核心');
    say(902, '刚才说的聚餐日期错了');
    await sleep(400);
    say(903, '是后天下午三点');
    await wait(() => existsSync(join(state, 'decisions.jsonl')));
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].segment.map((r: any) => r.text), ['@999', '刚才说的聚餐日期错了', '是后天下午三点']);
    assert.deepEqual(calls[0].segment.map((r: any) => r.who), ['10000420', '10000420', '10000420']);
    const decisions = (await readFile(join(state, 'decisions.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.equal(decisions.length, 1);
    assert.equal(decisions[0].machine.direct.rule, 'at');
    assert.equal(decisions[0].machine.direct.ordinal, 1, '三条消息合成一个段，不算三次机会');
    assert.deepEqual(actions, [], '验收核心沉默，不产生QQ发送动作');
  } finally {
    socket?.terminate(); child.kill('SIGTERM'); await once(child, 'exit');
    core.closeAllConnections(); await new Promise<void>(resolve => core.close(() => resolve()));
    await rm(temp, { recursive: true, force: true });
  }
});
