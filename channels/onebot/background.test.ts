import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { InboundRing } from './inbound-ids.ts';
import { loadPolicy } from './policy.ts';

test('旁听只取同群当前段之前的记录，重启不丢来源，不混当前及后来消息', () => {
  const temp = mkdtempSync(join(tmpdir(), 'qqagent-background-'));
  try {
    const path = join(temp, 'inbound.json'), ring = new InboundRing({ path });
    const base = { who: 10000420, name: '同名', at: '2026-11-07T12:00:00.000Z' };
    ring.add({ ...base, id: 1, text: '旧无来源' });
    ring.add({ ...base, id: 2, target: 'group:71', text: '聚餐在河边茶馆' });
    ring.add({ ...base, id: 3, target: 'group:72', text: '别群内容' });
    ring.add({ ...base, id: 4, target: 'user:10000420', text: '私聊内容' });
    ring.add({ ...base, id: 5, target: 'group:71', text: '改成周日', who: 10000421 });
    ring.add({ ...base, id: 50, target: 'group:71', text: ' \n\t ' });
    ring.add({ ...base, id: 6, target: 'group:71', text: '当前段第一条' });
    ring.add({ ...base, id: 7, target: 'group:71', text: '当前段第二条' });
    ring.add({ ...base, id: 8, target: 'group:71', text: '后来才收到的消息' });
    const before = readFileSync(path);
    const result = new InboundRing({ path }).contextBefore('group:71', [7, 6], loadPolicy().context);
    assert.deepEqual(result.messages.map(row => [row.who, row.text]), [
      ['10000420', '聚餐在河边茶馆'], ['10000421', '改成周日']]);
    assert.equal(result.omitted, 0);
    assert.deepEqual(readFileSync(path), before);
    assert.deepEqual(ring.contextBefore('group:71', [999], loadPolicy().context).messages, []);
    assert.deepEqual(ring.contextBefore('user:10000420', [4], loadPolicy().context).messages, []);
  } finally { rmSync(temp, { recursive: true, force: true }); }
});

test('旁听时效及预算保留完整新消息，并明确省略旧消息', () => {
  const temp = mkdtempSync(join(tmpdir(), 'qqagent-background-limit-'));
  try {
    const ring = new InboundRing({ path: join(temp, 'inbound.json') });
    const base = { who: 10000420, target: 'group:71', at: '2026-11-07T12:00:00.000Z' };
    ring.add({ ...base, id: 1, at: '2026-11-07T11:00:00Z', text: '过时内容' });
    ring.add({ ...base, id: 2, text: '很长的前文'.repeat(300) });
    ring.add({ ...base, id: 3, text: '最新完整前文' });
    ring.add({ ...base, id: 4, text: '当前话' });
    for (const limits of [{ maxMessages: 8, maxChars: 200, maxAgeMs: 300000 },
                          { maxMessages: 1, maxChars: 4000, maxAgeMs: 300000 }]) {
      const result = ring.contextBefore('group:71', [4], limits);
      assert.deepEqual(result.messages.map(row => row.text), ['最新完整前文']);
      assert.equal(result.omitted, 1);
      assert.ok(JSON.stringify(result.messages).length <= limits.maxChars);
    }
  } finally { rmSync(temp, { recursive: true, force: true }); }
});
