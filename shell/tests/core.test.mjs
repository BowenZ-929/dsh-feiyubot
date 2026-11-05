/**
 * 核的守护：探活 / 重启 / 退避 / 收尸，全部用注入的假进程、假探活、假计时器跑，
 * 一条也不真起 python，也不碰 127.0.0.1:8787。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { coreSettings, spawnArgs, startCoreSupervisor } from '../lib/core.js';

const SHELL_ROOT = '/repo/shell';

/** 手动推进的虚拟时钟：只跑注入的计时器，真定时器一条不用。 */
function makeScheduler() {
  let now = 0;
  let seq = 0;
  const jobs = new Map();
  const add = (fn, ms, period) => {
    const id = ++seq;
    jobs.set(id, { fn, at: now + ms, period });
    return id;
  };
  return {
    now: () => now,
    setTimeout: (fn, ms) => add(fn, ms, 0),
    clearTimeout: (id) => { jobs.delete(id); },
    setInterval: (fn, ms) => add(fn, ms, ms),
    clearInterval: (id) => { jobs.delete(id); },
    pending: () => [...jobs.values()].map((job) => ({ at: job.at, period: job.period })),
    advance(ms) {
      const end = now + ms;
      for (;;) {
        let best = null;
        for (const [id, job] of jobs) {
          if (job.at <= end && (best === null || job.at < best[1].at)) best = [id, job];
        }
        if (best === null) break;
        const [id, job] = best;
        now = job.at;
        if (job.period > 0) job.at = now + job.period;
        else jobs.delete(id);
        job.fn();
      }
      now = end;
    },
  };
}

/** 让 await 链上的微任务全部跑完（假探活、假退出都是立即兑现的 Promise）。 */
async function settle(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) await new Promise((done) => setImmediate(done));
}

/**
 * 假子进程：kill(signal) 记一笔并触发 exit 监听（默认连 SIGTERM 也走；
 * stubborn=true 时只有 SIGKILL 才真退，用来验宽限期后的强杀）。
 */
function makeSpawner({ stubborn = false } = {}) {
  const children = [];
  const spawn = (command, args, options) => {
    const child = {
      command,
      args,
      options,
      pid: 4200 + children.length,
      signals: [],
      listeners: [],
      errorListeners: [],
      emitError(message) {
        const waiting = this.errorListeners.splice(0);
        for (const listener of waiting) listener(new Error(message));
      },
      kill(signal) {
        this.signals.push(signal);
        if (signal === 'SIGTERM' && stubborn) return true;
        const waiting = this.listeners.splice(0);
        for (const listener of waiting) listener(null, signal);
        return true;
      },
      once(event, listener) {
        if (event === 'exit') this.listeners.push(listener);
        return this;
      },
      on(event, listener) {
        if (event === 'error') this.errorListeners.push(listener);
        return this;
      },
    };
    children.push(child);
    return child;
  };
  return { children, spawn };
}

function build({ probe, spawner, timers, options = {}, env = { QQAGENT_CORE_PORT: '8899' } }) {
  const logs = [];
  const asked = [];
  const supervisor = startCoreSupervisor({
    settings: coreSettings(env, SHELL_ROOT),
    spawn: spawner.spawn,
    probe: async (url, timeoutMs) => {
      asked.push({ url, timeoutMs });
      return probe.value;
    },
    timers,
    log: (message) => logs.push(message),
    ...options,
  });
  return { supervisor, logs, asked };
}

test('端口读环境变量：QQAGENT_CORE_PORT 推 UPSTREAM，QQAGENT_CORE_DIR 推核目录', () => {
  const fallback = coreSettings({}, SHELL_ROOT);
  const custom = coreSettings({ QQAGENT_CORE_PORT: '8899', QQAGENT_CORE_DIR: '/tmp/core' }, SHELL_ROOT);

  console.log('默认:', JSON.stringify(fallback));
  console.log('覆盖:', JSON.stringify(custom));

  // 默认**不给** --root：核自己解析 <仓库>/home 的实盘根。
  // 过去默认传 plugin/organs/soul/data（出厂默认），搬家后会被核的门禁拒绝启动（退出码 2）。
  assert.deepEqual(fallback, {
    port: 8787,
    coreDir: '/repo/plugin',
    root: undefined,
    upstream: 'http://127.0.0.1:8787',
  });
  assert.deepEqual(custom, {
    port: 8899,
    coreDir: '/tmp/core',
    root: undefined,
    upstream: 'http://127.0.0.1:8899',
  });
  assert.deepEqual(spawnArgs(custom), ['-m', 'api.devserver', '--port', '8899']);
  // 显式覆盖仍然生效（QQAGENT_SOUL_ROOT），但要由核的门禁复核它落在 home 下。
  const explicit = coreSettings({ QQAGENT_SOUL_ROOT: '/repo/home/organs/soul' }, SHELL_ROOT);
  assert.deepEqual(spawnArgs(explicit), ['-m', 'api.devserver', '--port', '8787', '--root', '/repo/home/organs/soul']);;
  assert.throws(() => coreSettings({ QQAGENT_CORE_PORT: 'haha' }, SHELL_ROOT), /QQAGENT_CORE_PORT/);
  assert.throws(() => coreSettings({ QQAGENT_CORE_PORT: '70000' }, SHELL_ROOT), /QQAGENT_CORE_PORT/);
});

test('已经有人在跑：探活通过就不 spawn，只在 /soul/check 上问一句', async () => {
  const timers = makeScheduler();
  const spawner = makeSpawner();
  const { supervisor, logs, asked } = build({ probe: { value: true }, spawner, timers });

  await supervisor.started;
  await settle();

  console.log('探活地址:', JSON.stringify(asked));
  console.log('日志:', JSON.stringify(logs));
  assert.deepEqual(asked.map((one) => one.url), ['http://127.0.0.1:8899/soul/check']);
  assert.equal(spawner.children.length, 0, '人手动起的核优先，壳不该再起一个');
  assert.equal(supervisor.state().spawned, 0);
  assert.equal(supervisor.state().healthy, true);
  // 探活是 5 秒一拍
  assert.deepEqual(timers.pending(), [{ at: 5000, period: 5000 }]);
  await supervisor.stop();
});

test('没人应答就先起一个：python3 -m api.devserver 在核目录里、端口与 --root 都对', async () => {
  const timers = makeScheduler();
  const spawner = makeSpawner();
  const { supervisor } = build({ probe: { value: false }, spawner, timers });

  await supervisor.started;
  await settle();

  const child = spawner.children[0];
  console.log('起的进程:', child.command, JSON.stringify(child.args), 'cwd=' + child.options.cwd);
  assert.equal(spawner.children.length, 1);
  assert.equal(child.command, 'python3');
  assert.deepEqual(child.args, ['-m', 'api.devserver', '--port', '8899']);
  assert.equal(child.options.cwd, '/repo/plugin');
  await supervisor.stop();
});

test('探活失败 → 收掉重启 → 退避 1s、2s、4s、封顶不再涨', async () => {
  const timers = makeScheduler();
  const spawner = makeSpawner();
  const probe = { value: false };
  const { supervisor, logs } = build({ probe, spawner, timers, options: { backoffMaxMs: 4000 } });

  await supervisor.started;
  await settle();
  assert.equal(spawner.children.length, 1, '启动时没人应答，先起一个');

  const delays = [];
  for (const expected of [1000, 2000, 4000, 4000]) {
    await supervisor.check();
    await settle();
    assert.equal(supervisor.state().backoffMs, expected, '退避应该走到 ' + expected);
    delays.push(supervisor.state().backoffMs);
    assert.deepEqual(spawner.children.at(-1).signals, ['SIGTERM'], '不健康时壳只收自己起的那个');
    timers.advance(expected);
    await settle();
  }

  console.log('退避序列:', JSON.stringify(delays));
  console.log('起过的进程数:', spawner.children.length);
  console.log('日志尾:', JSON.stringify(logs.slice(-4)));
  assert.equal(spawner.children.length, 5, '1 次启动 + 4 次重启');
  assert.equal(supervisor.state().spawned, 5);
  await supervisor.stop();
});

test('核恢复健康：退避归零，下一次失败重新从 1s 开始', async () => {
  const timers = makeScheduler();
  const spawner = makeSpawner();
  const probe = { value: false };
  const { supervisor } = build({ probe, spawner, timers });

  await supervisor.started;
  await settle();
  await supervisor.check();
  await settle();
  timers.advance(1000);
  await settle();
  assert.equal(spawner.children.length, 2);

  probe.value = true;
  await supervisor.check();
  await settle();
  console.log('恢复健康后状态:', JSON.stringify(supervisor.state()));
  assert.equal(supervisor.state().healthy, true);
  assert.equal(supervisor.state().backoffMs, 0);

  probe.value = false;
  await supervisor.check();
  await settle();
  assert.equal(supervisor.state().backoffMs, 1000, '归零之后再失败要重新从 1s 起');
  await supervisor.stop();
});

test('壳收摊：SIGTERM 收掉子进程，之后一拍也不再起', async () => {
  const timers = makeScheduler();
  const spawner = makeSpawner();
  const { supervisor } = build({ probe: { value: false }, spawner, timers });

  await supervisor.started;
  await settle();
  const child = spawner.children[0];

  await supervisor.stop();
  console.log('子进程收到的信号:', JSON.stringify(child.signals));
  assert.deepEqual(child.signals, ['SIGTERM'], '体面退出只要 SIGTERM，不该升级成 SIGKILL');
  assert.equal(supervisor.state().running, false);
  assert.equal(supervisor.state().childPid, undefined);

  timers.advance(60000);
  await settle();
  assert.equal(spawner.children.length, 1, '收摊之后不许再起进程');
  assert.deepEqual(timers.pending(), [], '计时器也要清干净，别把宿主留住');
});

test('核赖着不走：宽限期后 SIGKILL，清理照样兑现', async () => {
  const timers = makeScheduler();
  const spawner = makeSpawner({ stubborn: true });
  const { supervisor } = build({ probe: { value: false }, spawner, timers, options: { killGraceMs: 3000 } });

  await supervisor.started;
  await settle();
  const child = spawner.children[0];

  let stopped = false;
  const stopping = supervisor.stop().then(() => { stopped = true; });
  await settle();
  assert.deepEqual(child.signals, ['SIGTERM']);
  assert.equal(stopped, false, '3000ms 宽限期内应该还在等');

  timers.advance(3000);
  await settle();
  await stopping;
  console.log('子进程收到的信号:', JSON.stringify(child.signals), '| stopped:', stopped);
  assert.deepEqual(child.signals, ['SIGTERM', 'SIGKILL']);
  assert.equal(stopped, true);
});

test('核起不来（python3 不在 / cwd 不存在）：不带走宿主，记一句，退避后重试', async () => {
  const timers = makeScheduler();
  const spawner = makeSpawner();
  const { supervisor, logs } = build({ probe: { value: false }, spawner, timers });

  await supervisor.started;
  await settle();
  const first = spawner.children[0];

  // spawn 的失败是异步的 'error' 事件；没人接住它就 throw，宿主跟着一起死
  first.emitError('spawn python3 ENOENT');
  await settle();

  console.log('起不来之后的日志:', JSON.stringify(logs.slice(-2)));
  assert.equal(supervisor.state().childPid, undefined, '起不来的进程不该还挂在状态里');
  assert.match(logs.at(-1), /核起不来：spawn python3 ENOENT/);

  await supervisor.check();
  await settle();
  assert.equal(supervisor.state().backoffMs, 1000);
  timers.advance(1000);
  await settle();
  assert.equal(spawner.children.length, 2, '退避到点之后要再试一次');
  await supervisor.stop();
});

