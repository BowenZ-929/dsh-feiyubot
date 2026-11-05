/**
 * 席位 smoke test：不依赖宿主，用假的 slots 服务 / react 调客户端半边的 apply，
 * 看它把哪几个席位挂到了哪个槽上；外加面板页那一侧的静态判据（BASE 只有一份推导、
 * 导航七项都落在核上真实路由）。
 *
 * 口径变更（2026-11-05 一页一席位）：DSH 里只剩**一个**席位「她的面板」（qqagent-panel，
 * order 90），三处槽复用同一个组件——一层 iframe，指向核的聚合页 /panel。
 * 同时删掉两条旧断言（旧形态的证据，对象已经不存在了）：
 *   1. 「心与脑是只读页：未接入时只有一句「未接入」」——壳里不再有心/脑页面组件；
 *   2. 「三处席位共用一份轮询：一个计时器，一趟拉两个端点」——壳不再轮询 /qqagent/heart
 *      与 /qqagent/brain；心与脑的页在核那一侧、在聚合页的 iframe 里，各自取自己的数。
 * 不是「改绿」，是那两条断言描述的东西被收掉了：接口形态变了，证据跟着换。
 * 宿主里真加载的证据仍由 README 记的 curl 给出。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const require = createRequire(import.meta.url);
const manifest = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));

/** 最小 react：createElement 够拼出一棵可检查的元素树。 */
const h = (type, props, ...children) => ({ type, props: props ?? {}, children });
let cleanups = [];
const react = {
  createElement: h,
  useState: (initial) => [initial, () => {}],
  useEffect: (effect) => {
    const off = effect();
    if (typeof off === 'function') cleanups.push(off);
  },
};

let loaded;
globalThis.window = {
  __ModuleLoader__: {
    load(entry) {
      loaded = { id: entry.id, module: entry.factory(() => react) };
    },
  },
};

/** 默认的假 fetch：留着，免得将来有页面组件在 apply 时真发请求。 */
globalThis.fetch = async () => ({ json: async () => ({ ok: false, data: null, errors: [] }) });

await import('../client/index.js');

/** 把一个元素树摊平成类型与文本。 */
function walk(node, out = { types: [], text: '' }) {
  if (node === null || node === undefined) return out;
  if (typeof node === 'string' || typeof node === 'number') {
    out.text += String(node);
    return out;
  }
  if (Array.isArray(node)) {
    for (const one of node) walk(one, out);
    return out;
  }
  out.types.push(node.type);
  if (node.props?.src) out.text += ' src=' + node.props.src;
  walk(node.children, out);
  return out;
}

function fakeContext() {
  const registered = [];
  const injected = [];
  const effects = [];
  const ctx = {
    slots: {
      inject(slotName, register) {
        injected.push(slotName);
        register();
        return () => {};
      },
      register(spec, component) {
        registered.push({ spec, component });
        return () => {};
      },
    },
    effect(callback, label) {
      effects.push({ label, off: callback() });
    },
  };
  return { ctx, registered, injected, effects };
}

test('产物是脚本不是 ES 模块（多一行 export {} 宿主就加载不了）', () => {
  const built = readFileSync(new URL('../client/index.js', import.meta.url), 'utf8');
  const offenders = built.split('\n').filter((line) => /^(export|import)\b/.test(line));
  console.log('产物顶层的 import/export 行数:', offenders.length);
  assert.deepEqual(offenders, []);
});

test('模块 id 与包名一致（宿主按包名注册 boot 图行）', () => {
  console.log('模块 id:', loaded.id, '| 包名:', manifest.name);
  assert.equal(loaded.id, manifest.name);
  assert.equal(manifest.dsh.client.platform, 'web');
});

test('只有一个席位「她的面板」：三处槽复用同一个组件', () => {
  const { ctx, registered, injected, effects } = fakeContext();
  loaded.module.apply(ctx);

  console.log('injected 槽:', JSON.stringify(injected));
  for (const one of registered) {
    console.log('seat:', JSON.stringify(one.spec));
  }

  const panel = registered.filter((one) => one.spec.name === 'sidebar.panellist');
  const main = registered.filter((one) => one.spec.name === 'main');
  const section = registered.filter((one) => one.spec.name === 'settings.section');

  assert.equal(panel.length, 1, '侧栏只该有一个席位');
  assert.equal(main.length, 1, '主区只该有一个席位');
  assert.equal(section.length, 1, '设置页只该有一个席位');
  assert.equal(registered.length, 3, '一个席位 × 三处槽 = 三条登记，不多不少');

  const ids = ['qqagent-panel'];
  assert.deepEqual(panel.map((one) => one.spec.id), ids);
  assert.deepEqual(panel.map((one) => one.spec.order), [90]);
  assert.deepEqual(panel.map((one) => one.spec.label), ['她的面板']);
  assert.deepEqual(section.map((one) => one.spec.id), ids);
  assert.deepEqual(section.map((one) => one.spec.label), ['她的面板']);
  // 侧栏 id 必须能在 main 的 keyed 槽里找到同名登记，否则点侧栏会抛
  assert.deepEqual(main.map((one) => one.spec.key), ids);

  // 主区与设置页必须是同一份组件：一页一个席位那种形态不许再长回来
  assert.equal(main[0].component, section[0].component, '两处槽该复用同一个组件');
  // 每加一页就多挂一个席位的老路：标签里只许出现席位自己的名字
  for (const one of registered) {
    if (one.spec.label !== undefined) assert.equal(one.spec.label, '她的面板');
  }
  assert.equal(effects.length, 1, '席位卸载应该只登记一个 effect');
  assert.match(effects[0].label, /席位/);
});

test('席位只有一层 iframe，指向核的聚合页；壳里没有导航', () => {
  const { ctx, registered } = fakeContext();
  loaded.module.apply(ctx);
  const seat = registered.find((one) => one.spec.name === 'main' && one.spec.key === 'qqagent-panel');
  const tree = walk(seat.component());
  console.log('面板元素:', JSON.stringify(tree.types), seat.component().props.src);
  assert.deepEqual(tree.types, ['iframe'], '席位里除了 iframe 什么都不该有');
  // 壳的 /qqagent/panel 前缀剥掉才到核：核的 /panel 在壳里就是 /qqagent/panel/panel
  assert.equal(seat.component().props.src, '/qqagent/panel/panel');
  assert.match(tree.text, /src=\/qqagent\/panel\/panel/);
  // 导航不是壳搬的：席位里没有 a / nav / button
  for (const type of tree.types) assert.ok(!['a', 'nav', 'button', 'ul'].includes(type), '壳里出现了导航元素：' + type);
});

// ——— 面板页那一侧（核发的静态页），判据只看源码，不起服务器 ———

const PAGES_DIR = new URL('../../plugin/pages/', import.meta.url).pathname;
const PAGES_PY = new URL('../../plugin/api/pages.py', import.meta.url).pathname;

/** api/pages.py 的 PAGE_FILES：路径 → 文件名（只抓 "/...": "...." 这种键值）。 */
function pageFiles() {
  const text = readFileSync(PAGES_PY, 'utf8');
  const body = text.slice(text.indexOf('PAGE_FILES'));
  const map = new Map();
  for (const m of body.matchAll(/"(\/[^"]*)":\s*"([^"]+)"/g)) map.set(m[1], m[2]);
  return map;
}

function readPage(name) {
  return readFileSync(join(PAGES_DIR, name), 'utf8');
}

const FIVE = [['soul.html', 'soul'], ['heart.html', 'heart'], ['brain.html', 'brain'],
              ['home.html', 'home'], ['chat.html', 'chat']];

test('五页的 MOUNT/BASE 只有一份推导：页内 grep "var MOUNT|var BASE" 命中 0', () => {
  for (const [file, name] of FIVE) {
    const text = readPage(file);
    const dup = text.match(/^.*\bvar (MOUNT|BASE)\b.*$/gm) ?? [];
    console.log(file, '页内 self-derived MOUNT/BASE 行数:', dup.length, '| data-page:', name);
    assert.deepEqual(dup, [], file + ' 里还留着自己那份 MOUNT/BASE：' + JSON.stringify(dup));
    // 相对地址 + 自报页面名，两种打开方式下都对
    assert.ok(text.includes('<script src="panel-base.js" data-page="' + name + '"></script>'),
      file + ' 没有用相对地址引入 panel-base.js 并声明 data-page="' + name + '"');
    // 取数一律走公共片段给的 panelFetch
    assert.match(text, /panelFetch\(/, file + ' 没有走 panelFetch');
    assert.ok(!/\bfetch\(\s*"[^"]*"/.test(text.replace(/panelFetch\(/g, 'X')),
      file + ' 里还有裸 fetch("...") —— 那就绕开了挂载前缀');
  }
});

test('panel-base.js：两种挂载下都算得出同一个 BASE，panelFetch 把前缀补回去', () => {
  const source = readFileSync(join(PAGES_DIR, 'panel-base.js'), 'utf8');

  function mountAt(pathname, page) {
    const calls = [];
    const win = {};
    const document = { currentScript: { getAttribute: (key) => (key === 'data-page' ? page : null) } };
    const location = { pathname };
    const fetch = (url, init) => { calls.push({ url, init }); return Promise.resolve({}); };
    // 片段只碰这四个全局：按参数传进去，等于一次干净的挂载
    new Function('window', 'document', 'location', 'fetch', source)(win, document, location, fetch);
    return { win, calls };
  }

  const cases = [
    // 直开核：BASE 是空串，路径原样打到核
    ['/heart', 'heart', '', '/heart/mood', '/heart/mood'],
    // 壳 iframe：剥掉 /heart 之后剩的就是挂载前缀
    ['/qqagent/panel/heart', 'heart', '/qqagent/panel', '/heart/mood', '/qqagent/panel/heart/mood'],
    ['/', 'soul', '', '/soul/render', '/soul/render'],
    ['/qqagent/panel/', 'soul', '/qqagent/panel', '/soul/render', '/qqagent/panel/soul/render'],
    ['/home', 'home', '', '/home/rooms', '/home/rooms'],
    ['/qqagent/panel/home', 'home', '/qqagent/panel', '/home/rooms', '/qqagent/panel/home/rooms'],
    ['/day', 'day', '', '/day/plan', '/day/plan'],
    ['/qqagent/panel/panel', 'panel', '/qqagent/panel', '/x', '/qqagent/panel/x'],
  ];
  for (const [pathname, page, base, path, wanted] of cases) {
    const { win, calls } = mountAt(pathname, page);
    console.log(pathname, '→ PANEL_BASE =', JSON.stringify(win.PANEL_BASE));
    assert.equal(win.PANEL_BASE, base, pathname + ' 上的 base 算错了');
    win.panelFetch(path, { cache: 'no-store' });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, wanted, pathname + ' 上的取数地址算错了');
    assert.deepEqual(calls[0].init, { cache: 'no-store' }, 'panelFetch 要原样带上 init');
  }
});

test('panel.html 的七条导航都落在核上真实路由上（点不出 404）', () => {
  const files = pageFiles();
  const html = readPage('panel.html');
  const nav = [...html.matchAll(/<a href="([^"]+)"[^>]*target="organ"[^>]*>([^<]+)<\/a>/g)];
  console.log('导航:', JSON.stringify(nav.map((m) => m[2] + ' → ' + m[1])));
  assert.equal(nav.length, 7, '导航该有七项：魂 / 心 / 脑 / 家 / 日子 / 聊天 / 设置');

  const live = nav.map((m) => new URL(m[1], 'http://core/panel').pathname);
  console.log('解析到核上的路径:', JSON.stringify(live));
  assert.deepEqual(live, ['/', '/heart', '/brain', '/home', '/day', '/chat', '/settings']);
  for (const path of live) {
    assert.ok(files.has(path), '导航项 ' + path + ' 在 PAGE_FILES 里没有登记（点了就是 404）');
    assert.doesNotThrow(() => readFileSync(join(PAGES_DIR, files.get(path))),
      path + ' 登记的文件读不到：' + files.get(path));
  }
  // iframe 也在同一张路由表里：src="./" 从 /panel 解析到 /
  assert.ok(files.has('/'), '/ 必须仍是一张页面（iframe 的默认 src）');
  assert.match(html, /<iframe name="organ"/, '导航用 target="organ"，得有个同名的 iframe 接住');

  // 日子与设置这一轮没有真实页面：占位页要写明「还没接」，不许是一片空白
  for (const name of ['day.html', 'settings.html']) {
    assert.match(readPage(name), /还没接/, name + ' 该写一句「还没接」');
  }
  // 片段本身也是一条要发出去的静态文件，且必须是 javascript 的 MIME（发成 html 浏览器不执行）
  assert.ok(files.has('/panel-base.js'), 'PAGE_FILES 里没有 /panel-base.js');
  assert.match(readFileSync(PAGES_PY, 'utf8'), /"\.js": "text\/javascript/,
    'api/pages.py 没给 .js 单独的 content-type');
});

test('旧的三个席位不再注册：qqagent-soul / heart / brain 一个都不许出现', () => {
  const { ctx, registered } = fakeContext();
  loaded.module.apply(ctx);
  const ids = registered.map((one) => one.spec.id ?? one.spec.key);
  console.log('登记过的 id/key:', JSON.stringify(ids));
  for (const old of ['qqagent-soul', 'qqagent-heart', 'qqagent-brain']) {
    assert.ok(!ids.includes(old), '旧席位 ' + old + ' 还在注册');
  }
});
