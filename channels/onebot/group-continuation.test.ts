import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type ServerResponse } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import WebSocket from 'ws';

for (const [samePerson, delayedQuote, sampled, actionResult, command] of [
  [true,false,false,false,false], [false,false,false,false,false], [true,true,false,false,false],
  [true,false,true,false,false], [true,false,false,true,false], [true,false,false,false,true],
]) test(`活动回合：同人=${samePerson} 延迟引用=${delayedQuote} 抽样=${sampled} 动作结果=${actionResult} 命令=${command}`, { timeout: 25000 }, async () => {
  const temp = await mkdtemp(join(tmpdir(), 'qqagent-group-continuation-'));
  const calls: any[] = [], actions: any[] = [], receipts: any[] = [];
  let pending: ServerResponse | undefined, quoteRequest: any;
  const core = createServer(async (req, res) => {
    let raw = ''; for await (const part of req) raw += part;
    if (req.url === '/chat/ack') { receipts.push(JSON.parse(raw)); res.end('{"ok":true}'); return; }
    calls.push(JSON.parse(raw));
    res.setHeader('content-type', 'application/json');
    if (calls.length === 1) { pending = res; return; }
    res.end('{"ok":true,"chunks":[]}');
  });
  core.listen(0, '127.0.0.1'); await once(core, 'listening');
  const probe = createServer(); probe.listen(0, '127.0.0.1'); await once(probe, 'listening');
  const port = (probe.address() as { port: number }).port;
  await new Promise<void>(resolve => probe.close(() => resolve()));
  const state = join(temp, 'state');
  if (sampled) { await mkdir(state); await writeFile(join(state, 'segments.json'), JSON.stringify({ counters: { 'group:10000071': 9 } })); }
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
    socket.on('message', raw => {
      const frame = JSON.parse(String(raw));
      if (frame.action === 'get_msg') quoteRequest = frame; else {
        actions.push(frame); socket!.send(JSON.stringify({echo:frame.echo,status:'ok',retcode:0,data:{message_id:1001}}));
      }
    });
    const say = (id: number, text: string, at = false, group = 10000071, who = 10000420, quoted = false) => socket!.send(JSON.stringify({
      post_type: 'message', message_type: 'group', self_id: 999, group_id: group,
      user_id: who, sender: { user_id: who, card: '测试群友' }, message_id: id,
      message: [...(quoted ? [{ type: 'reply', data: { id: '12345' } }] : []), ...(at ? [{ type: 'at', data: { qq: '999' } }] : []), { type: 'text', data: { text } }],
    }));
    const finishFirst = () => pending!.end(JSON.stringify({ok:true, chunks:[{text:'旧答复',send_at:0}],
      delivery_id:'a'.repeat(32),reply_kind:actionResult?'action_result':'conversation'}));
    say(901, '明天下午见', !sampled);
    if (!sampled) say(900, '我在旁边聊午饭', false, 10000071, 10000421);
    await wait(() => !!pending);
    assert.equal(calls[0].who.id, 10000420, '首轮旁听者不能覆盖定向对象');
    assert.equal(calls[0].directed, !sampled, '核心需要区分定向回应与抽中的群内闲聊');
    say(902, command ? '/help' : '不是明天，是后天下午三点', false, 10000071, samePerson ? 10000420 : 10000421, delayedQuote);
    if (delayedQuote) {
      await wait(() => !!quoteRequest);
      finishFirst();
      await wait(() => existsSync(join(state, 'decisions.jsonl')));
      socket.send(JSON.stringify({ echo: quoteRequest.echo, status: 'ok', retcode: 0,
        data: { sender: { user_id: 10000420 }, message: [{ type: 'text', data: { text: '旧约定' } }] } }));
      await wait(() => logs.includes('key=mid:902'));
    }
    say(903, '另一位群友聊午饭', false, 10000071, 10000421);
    say(904, '另一个群的普通话', false, 10000072);
    await wait(() => logs.includes('key=mid:904'));
    if (!delayedQuote) finishFirst();
    await wait(() => existsSync(join(state, 'decisions.jsonl')) && readFileSync(join(state, 'decisions.jsonl'), 'utf8').trim().split('\n').length === 3);
    assert.equal(calls.length, samePerson && !command ? 2 : 1);
    if (samePerson && !command) assert.equal(calls[1].who.id, 10000420, '续轮仍回应补充者');
    if (samePerson && !command) assert.deepEqual(calls[1].segment.map((r: any) => r.text), [(delayedQuote ? '（引用）' : '') + '不是明天，是后天下午三点', '另一位群友聊午饭']);
    // 同一合并段保留多人原话；仅他人的新段不能继承定向。
    say(905, '定向回合完成以后普通闲聊');
    await sleep(5300);
    assert.equal(calls.length, samePerson && !command ? 2 : 1);
    const decisions = (await readFile(join(state, 'decisions.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    const own = decisions.filter(r => r.machine.target === 'group:10000071');
    assert.deepEqual(own.map(r => r.machine.direct.rule), [sampled ? 'chitchat' : 'at', command ? null : samePerson ? 'continuation' : 'chitchat', 'chitchat']);
    assert.deepEqual(own.map(r => r.machine.direct.ordinal), sampled ? [10, 11, 12] : [1, 2, 3]);
    const other = decisions.find(r => r.machine.target === 'group:10000072');
    assert.equal(other.machine.reason_code, 'chitchat_skipped');
    const deferred = samePerson && !command && !actionResult;
    assert.equal(actions.length, deferred ? 0 : 1);
    assert.equal(receipts[0].text, deferred ? '' : '旧答复');
    assert.equal(receipts[0].stop_reason, deferred ? 'superseded' : undefined);
    assert.equal(own[0].machine.reason_code, deferred ? 'superseded' : 'sent');
  } finally {
    pending?.destroy(); socket?.terminate(); child.kill('SIGTERM'); await once(child, 'exit');
    core.closeAllConnections(); await new Promise<void>(resolve => core.close(() => resolve()));
    await rm(temp, { recursive: true, force: true });
  }
});
