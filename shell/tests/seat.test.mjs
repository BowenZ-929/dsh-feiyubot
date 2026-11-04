/**
 * 席位 smoke test：不依赖宿主，用假的 cordis Context / slots 服务 / react 调客户端半边的 apply，
 * 看它到底把哪几个席位挂到了哪个槽上、以及三处席位是不是共用一份轮询。
 * 这是「席位真的挂上了」在本包内的硬证据；真宿主里的加载证据由 README 记的 curl 给出。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

const require = createRequire(import.meta.url);
const manifest = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));

/** 最小 react：createElement 够拼出一棵可检查的元素树；useState 固定返回初值；useEffect 立即跑。 */
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

/** 默认的假 fetch：核这两个端点还没有数据，页面应该落成「未接入」而不是报错。 */
const realFetch = globalThis.fetch;
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

test('三处席位 × 三个页面：sidebar.panellist / main / settings.section', () => {
  const { ctx, registered, injected, effects } = fakeContext();
  loaded.module.apply(ctx);

  console.log('injected 槽:', JSON.stringify(injected));
  for (const one of registered) {
    console.log('seat:', JSON.stringify(one.spec));
  }

  const panel = registered.filter((one) => one.spec.name === 'sidebar.panellist');
  const main = registered.filter((one) => one.spec.name === 'main');
  const section = registered.filter((one) => one.spec.name === 'settings.section');

  assert.equal(panel.length, 3);
  assert.equal(main.length, 3);
  assert.equal(section.length, 3);
  assert.equal(registered.length, 9);

  const ids = ['qqagent-soul', 'qqagent-heart', 'qqagent-brain'];
  assert.deepEqual(panel.map((one) => one.spec.id), ids);
  assert.deepEqual(panel.map((one) => one.spec.order), [90, 91, 92]);
  assert.deepEqual(panel.map((one) => one.spec.label), ['魂', '心', '脑']);
  assert.deepEqual(section.map((one) => one.spec.id), ids);
  assert.deepEqual(section.map((one) => one.spec.label), ['魂', '心', '脑']);
  // 侧栏 id 必须能在 main 的 keyed 槽里找到同名登记，否则点侧栏会抛
  assert.deepEqual(main.map((one) => one.spec.key), ids);

  for (const one of section) {
    const tree = walk(one.component());
    for (const type of tree.types) assert.ok(type !== 'input' && type !== 'button' && type !== 'form' && type !== 'a', '设置页席位出现了控件：' + type);
  }
  assert.equal(effects.length, 1, '席位卸载应该只登记一个 effect');
  assert.match(effects[0].label, /席位/);
});

test('魂页是 iframe，指向核的面板页；壳里没有魂的展示逻辑', () => {
  const { ctx, registered } = fakeContext();
  loaded.module.apply(ctx);
  const soul = registered.find((one) => one.spec.name === 'main' && one.spec.key === 'qqagent-soul');
  const tree = walk(soul.component());
  console.log('魂页元素:', JSON.stringify(tree.types), soul.component().props.src);
  assert.deepEqual(tree.types, ['iframe']);
  assert.equal(soul.component().props.src, '/qqagent/panel');
  assert.match(tree.text, /src=\/qqagent\/panel/);
});

test('心与脑是只读页：未接入时只有一句「未接入」，页面上没有任何写控件', () => {
  const { ctx, registered } = fakeContext();
  loaded.module.apply(ctx);
  const controls = new Set(['input', 'textarea', 'button', 'select', 'a', 'form', 'object', 'embed']);
  for (const key of ['qqagent-heart', 'qqagent-brain']) {
    const seat = registered.find((one) => one.spec.name === 'main' && one.spec.key === key);
    const tree = walk(seat.component());
    console.log(key, '页:', JSON.stringify(tree.types), '| 文本:', tree.text);
    assert.match(tree.text, /未接入/);
    for (const type of tree.types) assert.ok(!controls.has(type), key + ' 页出现了控件：' + type);
  }
});

test('三处席位共用一份轮询：一个计时器，一趟拉两个端点', async () => {
  // 前面的测试挂过页面，先把它们留下的订阅全部卸掉，从零开始数计时器。
  for (const off of cleanups) off();
  cleanups = [];

  const realSetInterval = globalThis.setInterval;
  const realClearInterval = globalThis.clearInterval;
  const ticks = [];
  let timers = 0;
  let clears = 0;
  globalThis.setInterval = (fn, ms) => {
    timers += 1;
    ticks.push({ fn, ms });
    return { unref() {} };
  };
  globalThis.clearInterval = () => { clears += 1; };

  const asked = [];
  const mainFetch = async (path, options) => {
    asked.push({ path, options });
    return { json: async () => ({ ok: true, data: { mood: 'calm' }, errors: [] }) };
  };
  globalThis.fetch = mainFetch;

  try {
    const { ctx, registered } = fakeContext();
    loaded.module.apply(ctx);

    // 把心与脑在 main / settings.section 上的四个实例都挂起来
    for (const key of ['qqagent-heart', 'qqagent-brain']) {
      for (const slot of ['main', 'settings.section']) {
        const seat = slot === 'main'
          ? registered.find((one) => one.spec.name === 'main' && one.spec.key === key)
          : registered.find((one) => one.spec.name === 'settings.section' && one.spec.id === key);
        seat.component();
      }
    }
    await new Promise((done) => setImmediate(done));
    await new Promise((done) => setImmediate(done));

    console.log('计时器个数:', timers, '| 节拍:', JSON.stringify(ticks.map((one) => one.ms)));
    console.log('拉过的端点:', JSON.stringify(asked.map((one) => one.path)));
    assert.equal(timers, 1, '四个页面实例只该有一个计时器');
    assert.equal(ticks[0].ms, 3000);
    assert.deepEqual(asked.map((one) => one.path), ['/qqagent/heart', '/qqagent/brain']);
    assert.equal(asked[0].options.cache, 'no-store');

    // 再手动走一个节拍：仍然是这一份轮询
    globalThis.fetch = async (path, options) => {
      asked.push({ path, options });
      return { json: async () => ({ ok: true, data: { mood: 'warm' }, errors: [] }) };
    };
    ticks[0].fn();
    await new Promise((done) => setImmediate(done));
    await new Promise((done) => setImmediate(done));
    console.log('第二趟拉过的端点:', JSON.stringify(asked.map((one) => one.path)));
    assert.equal(asked.length, 4);
    assert.equal(timers, 1);

    // 最后一个页面卸载后计时器要停；再挂一个才是新的一份
    for (const off of cleanups) off();
    cleanups = [];
    assert.equal(clears, 1, '全部卸载后应该把计时器停掉');
    registered.find((one) => one.spec.name === 'main' && one.spec.key === 'qqagent-heart').component();
    assert.equal(timers, 2);
    for (const off of cleanups) off();
  } finally {
    globalThis.setInterval = realSetInterval;
    globalThis.clearInterval = realClearInterval;
    globalThis.fetch = realFetch;
  }
});
