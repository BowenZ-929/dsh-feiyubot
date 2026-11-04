import { createProxyHandler } from './proxy.js';
import { organRoute } from './organ.js';
import { coreSettings, startCoreSupervisor } from './core.js';
/**
 * 壳的宿主半边：把面板代理挂到宿主的 webServer 上，把三个席位交给浏览器半边，
 * 把心与脑两条只读端点挂出来（数据只经 HTTP 从核来，壳只搬与降级），并守护核那个进程。
 * 这里不读她的魂，也不替她回话；魂的展示与修改全在核那边。
 */
export const name = 'qqagent-shell';
/** 面板页前缀：iframe 从这里加载核自己的面板页（`/` 与 `/panel` 都是它）。 */
const PANEL_PREFIX = '/qqagent/panel';
/** 面板页里的绝对路径 /soul* 也要能落到核那边：`/qqagent/soul*` 剥掉 /qqagent 转过去。 */
const SOUL_ALIAS_PREFIX = '/qqagent/soul';
/** 核的面板页里写死了 /soul*，这一段也照原样转过去，前缀不剥。 */
const SOUL_PREFIX = '/soul';
/** 核的地址不写死：QQAGENT_CORE_PORT 推出来（默认 8787），见 core.ts。 */
let settings;
export function coreOf() {
    settings ??= coreSettings();
    return settings;
}
/** 面板页：/qqagent/panel -> 核的 / */
export function panelRoute(upstream = coreOf().upstream) {
    return { kind: 'prefix', path: PANEL_PREFIX, handler: createProxyHandler({ prefix: PANEL_PREFIX, upstream }) };
}
/** 面板页的别名：/qqagent/soul* -> 核的 /soul*（剥掉 /qqagent，只留 /soul 前缀） */
export function soulAliasRoute(upstream = coreOf().upstream) {
    return { kind: 'prefix', path: SOUL_ALIAS_PREFIX, handler: createProxyHandler({ prefix: '/qqagent', upstream }) };
}
/** 魂的接口：/soul* -> 核的 /soul*（前缀不剥；核的面板页里用的是这个绝对路径） */
export function soulRoute(upstream = coreOf().upstream) {
    return { kind: 'prefix', path: SOUL_PREFIX, handler: createProxyHandler({ prefix: '', upstream }) };
}
/** 心与脑：两条只读端点，非 GET/HEAD 一律 405，每次请求重读核。 */
export function organRoutes(upstream = coreOf().upstream) {
    return [
        organRoute({ organ: 'heart', upstream }),
        organRoute({ organ: 'brain', upstream }),
    ];
}
/** 全部要挂上的路由；顺序只为可读，宿主的 prefix 匹配是「最长前缀先赢」。 */
export function allRoutes(upstream = coreOf().upstream) {
    return [panelRoute(upstream), soulAliasRoute(upstream), soulRoute(upstream), ...organRoutes(upstream)];
}
export function apply(ctx) {
    ctx.inject(['webServer'], (webCtx) => {
        const webServer = webCtx.webServer;
        if (webServer === undefined)
            return;
        const core = coreOf();
        for (const route of allRoutes(core.upstream)) {
            webCtx.effect(() => webServer.register(route), `qqagent ${route.path} 路由`);
        }
        // 核的守护：先探活（人手动起的核优先），没人应答就自己起一个，壳收摊时一起收掉。
        webCtx.effect(() => {
            const supervisor = startCoreSupervisor({
                settings: core,
                log: (message) => console.log(`[qqagent-shell] ${message}`),
            });
            return () => {
                void supervisor.stop();
            };
        }, 'qqagent 核的守护');
    });
}
