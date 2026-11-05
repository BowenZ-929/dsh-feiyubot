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
/** 环境变量 → 核的坐标。端口读坏了就抛（宁可宿主日志里一句明白话，也别静默连错端口）。 */
export function coreSettings(env = process.env, shellRoot = SHELL_ROOT) {
    const rawPort = env.QQAGENT_CORE_PORT?.trim();
    const port = rawPort === undefined || rawPort === '' ? DEFAULT_CORE_PORT : Number(rawPort);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error(`QQAGENT_CORE_PORT 不是端口：${rawPort}（要 1-65535 的整数）`);
    }
    const rawDir = env.QQAGENT_CORE_DIR?.trim();
    const coreDir = rawDir === undefined || rawDir === '' ? resolve(shellRoot, '..', 'plugin') : resolve(rawDir);
    const rawSoulRoot = env.QQAGENT_SOUL_ROOT?.trim();
    const root = rawSoulRoot === undefined || rawSoulRoot === '' ? undefined : resolve(rawSoulRoot);
    return { port, coreDir, root, upstream: `http://127.0.0.1:${port}` };
}
export function spawnArgs(settings) {
    const args = ['-m', 'api.devserver', '--port', String(settings.port)];
    // 默认**不传** --root：核自己解析 `<仓库>/home` 的实盘根。
    // 壳过去默认传 plugin/organs/soul/data，那是「出厂默认」，会被核的门禁拒绝启动（退出码 2）——
    // 搬家之后这条路就断了，所以改成只有显式给了 QQAGENT_SOUL_ROOT 才传。
    if (settings.root)
        args.push('--root', settings.root);
    return args;
}
/** 默认计时器：都 unref，壳不该因为一个探活节拍把宿主留住。 */
const nodeTimers = {
    setTimeout(callback, ms) {
        const handle = setTimeout(callback, ms);
        handle.unref();
        return handle;
    },
    clearTimeout(handle) {
        clearTimeout(handle);
    },
    setInterval(callback, ms) {
        const handle = setInterval(callback, ms);
        handle.unref();
        return handle;
    },
    clearInterval(handle) {
        clearInterval(handle);
    },
};
/** 探活：200 + 一个 JSON 对象才算核；连不上、超时、非 200、非 JSON 一律算没应答。 */
export async function probeHealth(url, timeoutMs = PROBE_TIMEOUT_MS) {
    try {
        const response = await fetch(url, {
            headers: { accept: 'application/json' },
            redirect: 'error',
            signal: AbortSignal.timeout(timeoutMs),
        });
        if (!response.ok)
            return false;
        const parsed = JSON.parse(await response.text());
        return parsed !== null && typeof parsed === 'object';
    }
    catch {
        return false;
    }
}
const defaultSpawn = (command, args, options) => spawn(command, args, options);
/**
 * 起一个守护。返回的 stop() 必须由 ctx.effect 的清理函数调用：先 SIGTERM，
 * killGraceMs 内没退再 SIGKILL，不留孤儿。
 */
export function startCoreSupervisor(options) {
    const settings = options.settings;
    const timers = options.timers ?? nodeTimers;
    const spawnProcess = options.spawn ?? defaultSpawn;
    const probe = options.probe ?? probeHealth;
    const log = options.log ?? (() => { });
    const pollMs = options.pollMs ?? POLL_MS;
    const killGraceMs = options.killGraceMs ?? KILL_GRACE_MS;
    const backoffMax = options.backoffMaxMs ?? BACKOFF_MAX_MS;
    const state = { running: false, healthy: false, backoffMs: 0, spawned: 0, childPid: undefined };
    let child = null;
    let nextBackoff = options.backoffBaseMs ?? BACKOFF_BASE_MS;
    let pollHandle = null;
    let restartHandle = null;
    let checking = false;
    let warnedMissingDir = false;
    const healthUrl = settings.upstream + HEALTH_PATH;
    function spawnChild() {
        if (!state.running || child !== null)
            return;
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
                if (child !== started)
                    return;
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
        }
        catch (error) {
            child = null;
            state.childPid = undefined;
            log(`核起不来：${error instanceof Error ? error.message : String(error)}`);
        }
    }
    /** 只收壳自己起的那个；返回的 Promise 在它退出或宽限期用完时兑现。 */
    function killChild() {
        const dying = child;
        if (dying === null)
            return Promise.resolve();
        child = null;
        state.childPid = undefined;
        return new Promise((done) => {
            let finished = false;
            const finish = () => {
                if (finished)
                    return;
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
    function scheduleRestart() {
        if (!state.running || restartHandle !== null)
            return;
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
    async function healthyNow() {
        try {
            return await probe(healthUrl, PROBE_TIMEOUT_MS);
        }
        catch {
            return false;
        }
    }
    async function check() {
        if (!state.running || checking)
            return;
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
        }
        finally {
            checking = false;
        }
    }
    async function start() {
        if (state.running)
            return;
        state.running = true;
        log(`核的守护启动：${settings.upstream}，核目录 ${settings.coreDir}`);
        const healthy = await healthyNow();
        state.healthy = healthy;
        if (!state.running)
            return;
        if (healthy) {
            log('核已经在跑（人起的优先），壳不重复起');
        }
        else {
            spawnChild();
        }
        pollHandle = timers.setInterval(() => {
            void check();
        }, pollMs);
    }
    async function stop() {
        if (!state.running)
            return;
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
    function snapshot() {
        return { ...state };
    }
    const started = start();
    return { started, check, stop, state: snapshot };
}
