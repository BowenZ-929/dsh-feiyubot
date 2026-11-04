import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * 核的守护：壳启动时先探活，没人应答才自己起一个 Python 核；之后每 5 秒探一次，
 * 不健康就按 1s→2s→4s→…（上限 30s）退避重启，核恢复健康后退避归零；壳收摊时把子进程一起带走。
 *
 * 两条硬规矩：
 * - **人手动起的核优先**：探活通过就一个进程也不起，只在它死掉之后才接手；
 * - **只认自己的子进程**：不是壳起的那个永远不 kill（壳只管自己养的那一个）。
 *
 * 端口不写死：QQAGENT_CORE_PORT（默认 8787）推 UPSTREAM，QQAGENT_CORE_DIR（默认 <repo>/plugin）
 * 是核的工作目录，--root 是它下面的 organs/soul/data。
 */
export const DEFAULT_CORE_PORT = 8787;
/** 探活节拍：5 秒问一次「你还好吗」。 */
const POLL_MS = 5000;
/** 核只在本机，1 秒不回就当没应答，别把探活拖成 15 秒。 */
const PROBE_TIMEOUT_MS = 1000;
/** 重启退避：1s → 2s → 4s → … 到 30s 封顶。 */
const BACKOFF_BASE_MS = 1000;
const BACKOFF_MAX_MS = 30000;
/** SIGTERM 之后给核 3 秒体面退出，还不走就 SIGKILL。 */
const KILL_GRACE_MS = 3000;
/** 核的健康检查端点（核在 api/soul.py 的 SOUL_ROUTES 里给了它）。 */
const HEALTH_PATH = '/soul/check';

const SHELL_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export interface CoreSettings {
  /** 核监听的端口。 */
  port: number;
  /** 核的工作目录（python3 -m api.devserver 的 cwd）。 */
  coreDir: string;
  /** 魂的数据目录，--root 的值。 */
  root: string;
  /** 由端口推出来的核地址，例如 http://127.0.0.1:8787。 */
  upstream: string;
}

/** 环境变量 → 核的坐标。端口读坏了就抛（宁可宿主日志里一句明白话，也别静默连错端口）。 */
export function coreSettings(env: Record<string, string | undefined> = process.env, shellRoot: string = SHELL_ROOT): CoreSettings {
  const rawPort = env.QQAGENT_CORE_PORT?.trim();
  const port = rawPort === undefined || rawPort === '' ? DEFAULT_CORE_PORT : Number(rawPort);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`QQAGENT_CORE_PORT 不是端口：${rawPort}（要 1-65535 的整数）`);
  }
  const rawDir = env.QQAGENT_CORE_DIR?.trim();
  const coreDir = rawDir === undefined || rawDir === '' ? resolve(shellRoot, '..', 'plugin') : resolve(rawDir);
  return { port, coreDir, root: resolve(coreDir, 'organs', 'soul', 'data'), upstream: `http://127.0.0.1:${port}` };
}

export function spawnArgs(settings: CoreSettings): string[] {
  return ['-m', 'api.devserver', '--root', settings.root, '--port', String(settings.port)];
}

export interface CoreChild {
  pid?: number;
  kill(signal?: NodeJS.Signals): unknown;
  once?(event: 'exit', listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
  /** 起不来（python3 不在、cwd 不存在）是异步的 'error' 事件；不接住它会带走整个宿主。 */
  on?(event: 'error', listener: (error: Error) => void): unknown;
}

export type SpawnFunction = (command: string, args: string[], options: { cwd: string; stdio: 'inherit' }) => CoreChild;

export interface TimerApi {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  setInterval(callback: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

/** 默认计时器：都 unref，壳不该因为一个探活节拍把宿主留住。 */
const nodeTimers: TimerApi = {
  setTimeout(callback, ms) {
    const handle = setTimeout(callback, ms);
    handle.unref();
    return handle;
  },
  clearTimeout(handle) {
    clearTimeout(handle as NodeJS.Timeout);
  },
  setInterval(callback, ms) {
    const handle = setInterval(callback, ms);
    handle.unref();
    return handle;
  },
  clearInterval(handle) {
    clearInterval(handle as NodeJS.Timeout);
  },
};

/** 探活：200 + 一个 JSON 对象才算核；连不上、超时、非 200、非 JSON 一律算没应答。 */
export async function probeHealth(url: string, timeoutMs: number = PROBE_TIMEOUT_MS): Promise<boolean> {
  try {
    const response = await fetch(url, {
      headers: { accept: 'application/json' },
      redirect: 'error',
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) return false;
    const parsed: unknown = JSON.parse(await response.text());
    return parsed !== null && typeof parsed === 'object';
  } catch {
    return false;
  }
}

const defaultSpawn: SpawnFunction = (command, args, options) => spawn(command, args, options);

export interface SupervisorOptions {
  settings: CoreSettings;
  /** 起进程（测试注入假的，不真起 python）。 */
  spawn?: SpawnFunction;
  /** 探活（测试注入假的）。 */
  probe?: (url: string, timeoutMs: number) => Promise<boolean>;
  log?: (message: string) => void;
  timers?: TimerApi;
  pollMs?: number;
  killGraceMs?: number;
  backoffBaseMs?: number;
  backoffMaxMs?: number;
}

export interface SupervisorState {
  running: boolean;
  healthy: boolean;
  /** 下一次重启要等多久（毫秒）；0 表示没有待办退避。 */
  backoffMs: number;
  /** 壳一共起过几个核进程（含重启）。 */
  spawned: number;
  childPid: number | undefined;
}

export interface Supervisor {
  /** 已经开始（startCoreSupervisor 里就发起了，这里只是拿得到）。 */
  started: Promise<void>;
  check(): Promise<void>;
  stop(): Promise<void>;
  state(): SupervisorState;
}

/**
 * 起一个守护。返回的 stop() 必须由 ctx.effect 的清理函数调用：先 SIGTERM，
 * killGraceMs 内没退再 SIGKILL，不留孤儿。
 */
export function startCoreSupervisor(options: SupervisorOptions): Supervisor {
  const settings = options.settings;
  const timers = options.timers ?? nodeTimers;
  const spawnProcess = options.spawn ?? defaultSpawn;
  const probe = options.probe ?? probeHealth;
  const log = options.log ?? (() => {});
  const pollMs = options.pollMs ?? POLL_MS;
  const killGraceMs = options.killGraceMs ?? KILL_GRACE_MS;
  const backoffMax = options.backoffMaxMs ?? BACKOFF_MAX_MS;

  const state: SupervisorState = { running: false, healthy: false, backoffMs: 0, spawned: 0, childPid: undefined };
  let child: CoreChild | null = null;
  let nextBackoff = options.backoffBaseMs ?? BACKOFF_BASE_MS;
  let pollHandle: unknown = null;
  let restartHandle: unknown = null;
  let checking = false;
  let warnedMissingDir = false;

  const healthUrl = settings.upstream + HEALTH_PATH;

  function spawnChild(): void {
    if (!state.running || child !== null) return;
    if (!warnedMissingDir && !existsSync(settings.coreDir)) {
      warnedMissingDir = true;
      log(`核目录不存在：${settings.coreDir}（用 QQAGENT_CORE_DIR 指到真正的 plugin/）`);
    }
    const args = spawnArgs(settings);
    try {
      const started = spawnProcess('python3', args, { cwd: settings.coreDir, stdio: 'inherit' });
      child = started;
      state.spawned += 1;
      state.childPid = started.pid;
      log(`核没在跑，壳起了一个（pid ${started.pid ?? '?'}）：python3 ${args.join(' ')}（cwd ${settings.coreDir}）`);
      started.once?.('exit', (code, signal) => {
        if (child !== started) return;
        child = null;
        state.childPid = undefined;
        log(`壳起的核退出了（code ${code ?? '-'}，signal ${signal ?? '-'}）`);
      });
      started.on?.('error', (error) => {
        if (child === started) {
          child = null;
          state.childPid = undefined;
        }
        log(`核起不来：${error.message}`);
      });
    } catch (error) {
      child = null;
      state.childPid = undefined;
      log(`核起不来：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  /** 只收壳自己起的那个；返回的 Promise 在它退出或宽限期用完时兑现。 */
  function killChild(): Promise<void> {
    const dying = child;
    if (dying === null) return Promise.resolve();
    child = null;
    state.childPid = undefined;
    return new Promise<void>((done) => {
      let finished = false;
      const finish = (): void => {
        if (finished) return;
        finished = true;
        timers.clearTimeout(force);
        done();
      };
      const force = timers.setTimeout(() => {
        log(`核 ${killGraceMs}ms 内没退，SIGKILL`);
        dying.kill('SIGKILL');
        finish();
      }, killGraceMs);
      if (typeof dying.once !== 'function') {
        dying.kill('SIGTERM');
        finish();
        return;
      }
      dying.once('exit', finish);
      log('收核：SIGTERM');
      dying.kill('SIGTERM');
    });
  }

  function scheduleRestart(): void {
    if (!state.running || restartHandle !== null) return;
    const delay = Math.min(nextBackoff, backoffMax);
    state.backoffMs = delay;
    nextBackoff = Math.min(delay * 2, backoffMax);
    log(`核不健康，${delay}ms 后重启（退避上限 ${backoffMax}ms）`);
    restartHandle = timers.setTimeout(() => {
      restartHandle = null;
      state.backoffMs = 0;
      spawnChild();
    }, delay);
  }

  /** 探活抛错也只当没应答：守护不能因为一次奇怪的失败把自己挂掉。 */
  async function healthyNow(): Promise<boolean> {
    try {
      return await probe(healthUrl, PROBE_TIMEOUT_MS);
    } catch {
      return false;
    }
  }

  async function check(): Promise<void> {
    if (!state.running || checking) return;
    checking = true;
    try {
      const healthy = await healthyNow();
      state.healthy = healthy;
      if (healthy) {
        if (nextBackoff !== (options.backoffBaseMs ?? BACKOFF_BASE_MS) || state.backoffMs !== 0) {
          log('核恢复健康，退避归零');
        }
        nextBackoff = options.backoffBaseMs ?? BACKOFF_BASE_MS;
        state.backoffMs = 0;
        if (restartHandle !== null) {
          timers.clearTimeout(restartHandle);
          restartHandle = null;
        }
        return;
      }
      log(`核没有应答（${healthUrl}）`);
      await killChild();
      scheduleRestart();
    } finally {
      checking = false;
    }
  }

  async function start(): Promise<void> {
    if (state.running) return;
    state.running = true;
    log(`核的守护启动：${settings.upstream}，核目录 ${settings.coreDir}`);
    const healthy = await healthyNow();
    state.healthy = healthy;
    if (!state.running) return;
    if (healthy) {
      log('核已经在跑（人起的优先），壳不重复起');
    } else {
      spawnChild();
    }
    pollHandle = timers.setInterval(() => {
      void check();
    }, pollMs);
  }

  async function stop(): Promise<void> {
    if (!state.running) return;
    state.running = false;
    if (pollHandle !== null) {
      timers.clearInterval(pollHandle);
      pollHandle = null;
    }
    if (restartHandle !== null) {
      timers.clearTimeout(restartHandle);
      restartHandle = null;
    }
    await killChild();
    log('核的守护收摊');
  }

  function snapshot(): SupervisorState {
    return { ...state };
  }

  const started = start();
  return { started, check, stop, state: snapshot };
}
