import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import WebSocket from 'ws';

for (const scene of ['private', 'group']) for (const lookup of ['found', 'missing', 'timeout'])
test(`${scene} 引用${lookup}时更正仍按到达顺序入段`, { timeout: 15000 }, async () => {
  const temp = await mkdtemp(join(tmpdir(), 'qqagent-ingest-order-'));
  const calls: any[] = [];
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
  const child = spawn(process.execPath, ['server.ts', '--port', String(port), '--core',
    `http://127.0.0.1:${(core.address() as { port: number }).port}`, '--state', join(temp, 'state'),
    '--home', join(temp, 'home'), '--prefs', join(temp, 'prefs.jsonl')], {
    cwd: fileURLToPath(new URL('.', import.meta.url)),
    env: { ...process.env, QQAGENT_CHANNEL_MODE: 'send' }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let logs = '', socket: WebSocket | undefined, quoteRequest: any;
  child.stdout.on('data', x => { logs += x; }); child.stderr.on('data', x => { logs += x; });
  const wait = async (predicate: () => boolean) => {
    const deadline = Date.now() + 7000;
    while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
    assert.ok(predicate(), logs);
  };
  try {
    await wait(() => logs.includes('反向 WS 已监听'));
    socket = new WebSocket(`ws://127.0.0.1:${port}/ws`); await once(socket, 'open');
    socket.on('message', raw => {
      const frame = JSON.parse(String(raw));
      if (frame.action === 'get_msg') quoteRequest = frame;
    });
    const say = (id: number, text: string, quoted = false, who = 10000420) => socket!.send(JSON.stringify({
      post_type: 'message', message_type: scene, self_id: 999,
      ...(scene === 'group' ? {group_id: who === 10000420 ? 70001 : 70002} : {}),
      user_id: who, sender: {user_id: who, nickname: '测试者'}, message_id: id,
      message: [...(quoted ? [{type: 'reply', data: {id: '12345'}}] : []),
        ...(scene === 'group' ? [{type: 'at', data: {qq: '999'}}] : []),
        {type: 'text', data: {text}}],
    }));
    say(901, '那就周六去紫藤公园', true);
    await wait(() => !!quoteRequest);
    say(902, '不对，改成周日下午三点');
    say(903, '另一个会话的消息', false, 10000422);
    // 等确认已收到第二条，才归还第一条的引用资料。
    await wait(() => logs.includes('key=mid:903 →'));
    assert.ok(logs.includes('不对，改成周日下午三点'));
    assert.ok(!logs.includes('key=mid:901 →'), '引用未完成时，另一会话已可独立入段');
    if (lookup !== 'timeout') socket.send(JSON.stringify({echo: quoteRequest.echo,
      ...(lookup === 'found' ? {status: 'ok', retcode: 0,
        data: {sender: {user_id: 10000421}, message: [{type: 'text', data: {text: '周末去哪'}}]}}
        : {status: 'failed', retcode: 100, data: null})}));
    await wait(() => calls.some(row => row.who.id === 10000420));
    const current = calls.find(row => row.who.id === 10000420);
    const at = scene === 'group' ? '@999' : '';
    assert.deepEqual(current.segment.map((row: any) => row.text),
      [`（引用）${at}那就周六去紫藤公园`, `${at}不对，改成周日下午三点`]);
    assert.equal(current.segment[0].quote.source, lookup === 'found' ? 'get_msg' : 'none');
    if (lookup === 'found') assert.equal(current.segment[0].quote.text, '周末去哪');
    assert.equal(current.messageId, 902, '当前轮消息编号必须仍是后来的更正');
  } finally {
    socket?.terminate(); child.kill('SIGTERM'); await once(child, 'exit');
    core.closeAllConnections(); await new Promise<void>(resolve => core.close(() => resolve()));
    await rm(temp, {recursive: true, force: true});
  }
});
