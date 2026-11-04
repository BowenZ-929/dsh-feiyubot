import type { IncomingMessage, ServerResponse } from 'node:http';
import { type CoreSettings } from './core.js';
/**
 * 壳的宿主半边：把面板代理挂到宿主的 webServer 上，把三个席位交给浏览器半边，
 * 把心与脑两条只读端点挂出来（数据只经 HTTP 从核来，壳只搬与降级），并守护核那个进程。
 * 这里不读她的魂，也不替她回话；魂的展示与修改全在核那边。
 */
export declare const name = "qqagent-shell";
export declare function coreOf(): CoreSettings;
export interface PrefixRoute {
    kind: 'prefix';
    path: string;
    handler: (req: IncomingMessage, res: ServerResponse) => unknown;
}
/**
 * 宿主的 webServer 服务。核对的是 app.asar 里的
 * `@deepseek-ai/dsh-host-webserver/lib/index.js`：服务名 `webServer`（`super(ctx, "webServer")`），
 * 入参是 `{ kind, path, handler }`，返回卸载函数（`table.delete(route.path)`），
 * handler 可以是 async（宿主 `await route.handler(req, res)`）。
 */
export interface WebServer {
    register(route: PrefixRoute): () => void;
}
interface EffectContext {
    webServer: WebServer;
    effect(callback: () => () => void, label?: string): unknown;
}
interface Context {
    inject(deps: readonly string[], callback: (ctx: EffectContext) => unknown): unknown;
}
/** 面板页：/qqagent/panel -> 核的 / */
export declare function panelRoute(upstream?: string): PrefixRoute;
/** 面板页的别名：/qqagent/soul* -> 核的 /soul*（剥掉 /qqagent，只留 /soul 前缀） */
export declare function soulAliasRoute(upstream?: string): PrefixRoute;
/** 魂的接口：/soul* -> 核的 /soul*（前缀不剥；核的面板页里用的是这个绝对路径） */
export declare function soulRoute(upstream?: string): PrefixRoute;
/** 心与脑：两条只读端点，非 GET/HEAD 一律 405，每次请求重读核。 */
export declare function organRoutes(upstream?: string): PrefixRoute[];
/** 全部要挂上的路由；顺序只为可读，宿主的 prefix 匹配是「最长前缀先赢」。 */
export declare function allRoutes(upstream?: string): PrefixRoute[];
export declare function apply(ctx: Context): void;
export {};
