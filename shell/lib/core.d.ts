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
export declare const DEFAULT_CORE_PORT = 8787;
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
export declare function coreSettings(env?: Record<string, string | undefined>, shellRoot?: string): CoreSettings;
export declare function spawnArgs(settings: CoreSettings): string[];
export interface CoreChild {
    pid?: number;
    kill(signal?: NodeJS.Signals): unknown;
    once?(event: 'exit', listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
    /** 起不来（python3 不在、cwd 不存在）是异步的 'error' 事件；不接住它会带走整个宿主。 */
    on?(event: 'error', listener: (error: Error) => void): unknown;
}
export type SpawnFunction = (command: string, args: string[], options: {
    cwd: string;
    stdio: 'inherit';
}) => CoreChild;
export interface TimerApi {
    setTimeout(callback: () => void, ms: number): unknown;
    clearTimeout(handle: unknown): void;
    setInterval(callback: () => void, ms: number): unknown;
    clearInterval(handle: unknown): void;
}
/** 探活：200 + 一个 JSON 对象才算核；连不上、超时、非 200、非 JSON 一律算没应答。 */
export declare function probeHealth(url: string, timeoutMs?: number): Promise<boolean>;
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
export declare function startCoreSupervisor(options: SupervisorOptions): Supervisor;
