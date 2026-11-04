/**
 * 只读端点 smoke test：/qqagent/heart 与 /qqagent/brain。
 *
 * 照 dsh-affinity 的三条纪律守着：非 GET/HEAD 一律 405、每次请求重读核（壳里零缓存）、
 * 源读不到或坏掉就跳过并记进 errors[] 而不是整页崩。测试把壳的路由挂在裸 http.createServer 上
 * （宿主 webServer 的 kind:'prefix' 语义：handler 拿到完整 URL，自己决定怎么用）。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request } from 'node:http';
import { once } from 'node:events';
import { organRoutes } from '../lib/index.js';

function listen(server) {
  server.listen(0, '127.0.0.1');
  return once(server, 'listening').then(() => 'http://127.0.0.1:' + server.address().port);
}

/** 假核：handler 决定每条路径回什么；seen 记下它真的被问过几次。 */
async function fakeCore(handler) {
  const seen = [];
  const server = createServer((req, res) => {
    seen.push({ method: req.method, url: req.url });
    handler(req, res);
  });
  const origin = await listen(server);
  return { seen, origin, close: () => new Promise((done) => server.close(done)) };
}

/** 宿主 webServer 的 prefix 分发（长前缀先赢；handler 可以是 async）。 */
function mount(routes) {
  const server = createServer((req, res) => {
    const pathname = new URL(req.url ?? '/', 'http://127.0.0.1').pathname;
    let best;
    for (const route of routes) {
      if (pathname !== route.path && !pathname.startsWith(route.path + '/')) continue;
      if (best === undefined || route.path.length > best.path.length) best = route;
    }
    if (best === undefined) {
      res.writeHead(404).end();
      return;
    }
    Promise.resolve(best.handler(req, res)).catch(() => res.destroy());
  });
  return listen(server).then((origin) => ({ origin, close: () => new Promise((done) => server.close(done)) }));
}

const json = (res, status, body) => {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
};

test('非 GET/HEAD 一律 405，且不去打扰核', async () => {
  const core = await fakeCore((_req, res) => json(res, 200, { mood: 'calm' }));
  const shell = await mount(organRoutes(core.origin));

  for (const method of ['POST', 'PUT', 'DELETE', 'PATCH']) {
    for (const organ of ['heart', 'brain']) {
      const res = await fetch(shell.origin + '/qqagent/' + organ, { method });
      const body = await res.json();
      console.log(method + ' /qqagent/' + organ + ' ->', res.status, JSON.stringify(body));
      assert.equal(res.status, 405);
      assert.equal(body.ok, false);
      assert.equal(body.readonly, true);
      assert.match(body.message, /只收 GET\/HEAD/);
    }
  }

  assert.deepEqual(core.seen, [], '写方法一条都不该转到核那边');

  // GET 才是那条路
  const ok = await fetch(shell.origin + '/qqagent/heart');
  console.log('GET /qqagent/heart ->', ok.status, await ok.text());
  assert.equal(ok.status, 200);

  await shell.close();
  await core.close();
});

test('每请求重读：两次 GET 拿到核的两次回答，壳里没有缓存', async () => {
  let reads = 0;
  const core = await fakeCore((_req, res) => {
    reads += 1;
    json(res, 200, { beat: reads });
  });
  const shell = await mount(organRoutes(core.origin));

  const first = await fetch(shell.origin + '/qqagent/heart');
  const firstBody = await first.json();
  const second = await fetch(shell.origin + '/qqagent/heart');
  const secondBody = await second.json();

  console.log('第一趟:', JSON.stringify(firstBody), '| 第二趟:', JSON.stringify(secondBody));
  assert.equal(first.status, 200);
  assert.equal(firstBody.data.beat, 1);
  assert.equal(secondBody.data.beat, 2);
  assert.deepEqual(core.seen.map((one) => one.method + ' ' + one.url), ['GET /heart', 'GET /heart']);
  assert.equal(first.headers.get('cache-control'), 'no-store');

  await shell.close();
  await core.close();
});

test('坏源跳过不崩：核在 /heart 上给的是 HTML 只读页，心页降级成未接入并记 errors[]', async () => {
  const core = await fakeCore((req, res) => {
    if (req.url === '/heart') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end('<!doctype html><h1>心 · 只读</h1>');
      return;
    }
    json(res, 200, { schema: 'brain.v0', notes: [] });
  });
  const shell = await mount(organRoutes(core.origin));

  const heart = await fetch(shell.origin + '/qqagent/heart');
  const heartBody = await heart.json();
  const brain = await fetch(shell.origin + '/qqagent/brain');
  const brainBody = await brain.json();

  console.log('心 ->', heart.status, JSON.stringify(heartBody));
  console.log('脑 ->', brain.status, JSON.stringify(brainBody));

  assert.equal(heart.status, 200, '源坏掉也要回 200，页面不能崩');
  assert.equal(heartBody.ok, false);
  assert.equal(heartBody.data, null);
  assert.equal(heartBody.errors.length, 1);
  assert.match(heartBody.errors[0], /不是 JSON/);

  assert.equal(brain.status, 200);
  assert.equal(brainBody.ok, true);
  assert.equal(brainBody.data.schema, 'brain.v0');
  assert.deepEqual(brainBody.errors, []);

  await shell.close();
  await core.close();
});

test('核整台没起：两条端点都 200 + 可读 errors[]，壳不崩', async () => {
  const core = await fakeCore((_req, res) => json(res, 200, {}));
  const origin = core.origin;
  await core.close();

  const shell = await mount(organRoutes(origin));
  for (const organ of ['heart', 'brain']) {
    const res = await fetch(shell.origin + '/qqagent/' + organ);
    const body = await res.json();
    console.log('核没起 /qqagent/' + organ + ' ->', res.status, JSON.stringify(body));
    assert.equal(res.status, 200);
    assert.equal(body.ok, false);
    assert.equal(body.data, null);
    assert.match(body.errors[0], /核没有应答/);
  }
  await shell.close();
});

test('HEAD 只回头不收体', async () => {
  const core = await fakeCore((_req, res) => json(res, 200, { mood: 'calm' }));
  const shell = await mount(organRoutes(core.origin));

  const status = await new Promise((done, fail) => {
    const req = request(shell.origin + '/qqagent/brain', { method: 'HEAD' }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => done({ status: res.statusCode, body, type: res.headers['content-type'] }));
    });
    req.on('error', fail);
    req.end();
  });

  console.log('HEAD /qqagent/brain ->', JSON.stringify(status));
  assert.equal(status.status, 200);
  assert.equal(status.body, '');

  await shell.close();
  await core.close();
});
