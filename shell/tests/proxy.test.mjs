/**
 * 代理 smoke test：本地起一个假上游（回 {"ok":true} 并记下它看到的路径/方法/包体），
 * 把壳的 prefix 路由挂在一个裸 http.createServer 上（宿主 webServer 的 kind:'prefix' 语义就是
 * 「handler 拿到完整 URL，自己剥前缀」），验证转发正确、上游挂掉时回可读错误而不是崩进程。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { panelRoute, soulAliasRoute, soulRoute, allRoutes } from '../lib/index.js';

const PANEL_PREFIX = panelRoute().path;
const SOUL_PREFIX = soulRoute().path;

function listen(server) {
  server.listen(0, '127.0.0.1');
  return once(server, 'listening').then(() => 'http://127.0.0.1:' + server.address().port);
}

async function fakeCore() {
  const seen = [];
  const server = createServer((req, res) => {
    let body = '';
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => {
      seen.push({ method: req.method, url: req.url, body });
      res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify({ ok: true }));
    });
  });
  const origin = await listen(server);
  return { seen, origin, close: () => new Promise((done) => server.close(done)) };
}

/** 宿主 webServer 的 prefix 分发：长前缀先匹配。 */
function mount(routes) {
  const server = createServer((req, res) => {
    const pathname = new URL(req.url ?? '/', 'http://127.0.0.1').pathname;
    const route = routes.find((one) => pathname === one.path || pathname.startsWith(one.path + '/'));
    if (route === undefined) {
      res.writeHead(404, { 'content-type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify({ ok: false, message: '没有这个路由' }));
      return;
    }
    route.handler(req, res);
  });
  return listen(server).then((origin) => ({ origin, close: () => new Promise((done) => server.close(done)) }));
}

test('面板代理：/qqagent/panel/* 剥前缀转给核；/soul* 原样转；上游挂掉回可读错误', async () => {
  const core = await fakeCore();
  const shell = await mount(allRoutes(core.origin));

  // 1. 面板页：/qqagent/panel -> 核的 /
  const page = await fetch(shell.origin + '/qqagent/panel');
  console.log('GET /qqagent/panel ->', page.status, await page.text());

  // 2. 剥前缀：/qqagent/panel/soul -> 核的 /soul
  const soul = await fetch(shell.origin + '/qqagent/panel/soul');
  console.log('GET /qqagent/panel/soul ->', soul.status, await soul.text());

  // 3. 前缀透传：/soul/history?audience=mate -> 核的 /soul/history?audience=mate
  const history = await fetch(shell.origin + '/soul/history?audience=mate');
  console.log('GET /soul/history?audience=mate ->', history.status, await history.text());

  // 3b. 别名：/qqagent/soul* 剥掉 /qqagent，落到核的 /soul*
  const alias = await fetch(shell.origin + '/qqagent/soul');
  console.log('GET /qqagent/soul ->', alias.status, await alias.text());
  const aliasHistory = await fetch(shell.origin + '/qqagent/soul/history?audience=mate');
  console.log('GET /qqagent/soul/history?audience=mate ->', aliasHistory.status, await aliasHistory.text());

  // 3c. 只读端点也在同一张表里
  const heart = await fetch(shell.origin + '/qqagent/heart');
  console.log('GET /qqagent/heart ->', heart.status, await heart.text());

  // 4. POST 包体照搬（壳不改写她收到的东西）
  const edit = await fetch(shell.origin + '/qqagent/panel/soul/edit', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ actor: 'smoke', patch: { manner: '搬过来的一句' } }),
  });
  console.log('POST /qqagent/panel/soul/edit ->', edit.status, await edit.text());

  console.log('核看到的请求:', JSON.stringify(core.seen, null, 0));
  assert.deepEqual(core.seen.map((one) => one.method + ' ' + one.url), [
    'GET /',
    'GET /soul',
    'GET /soul/history?audience=mate',
    'GET /soul',
    'GET /soul/history?audience=mate',
    'GET /heart',
    'POST /soul/edit',
  ]);
  assert.equal(core.seen[6].body, JSON.stringify({ actor: 'smoke', patch: { manner: '搬过来的一句' } }));

  // 5. 不认识的路由：404，不是崩
  const miss = await fetch(shell.origin + '/nope');
  console.log('GET /nope ->', miss.status, await miss.text());
  assert.equal(miss.status, 404);

  // 6. 上游挂掉：502 + 可读错误，进程还活着
  await core.close();
  for (const attempt of [1, 2]) {
    const down = await fetch(shell.origin + '/qqagent/panel/soul');
    const body = await down.text();
    console.log('上游挂掉第 ' + attempt + ' 次 -> ' + down.status + ' ' + body);
    assert.equal(down.status, 502);
    assert.match(body, /ok":false/);
    assert.match(body, /核没有应答/);
  }

  await shell.close();
  console.log('代理进程在上游挂掉后仍能应答，且正常退出');
});
