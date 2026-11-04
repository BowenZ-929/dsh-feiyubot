import type { IncomingMessage, ServerResponse } from 'node:http';
/**
 * 心与脑的只读端点，形态照 dsh-affinity 的宿主路由：
 *
 * - 非 GET/HEAD 一律 405——入口存在，但没有写；
 * - 每次请求都重新向核要一次（无缓存、无 fs.watch、无版本号）；
 * - 源读不到 / 坏掉就跳过并记进 errors[]，页面照样 200，永不整页崩；
 * - 壳只搬字段与降级，不解释数据含义：核给什么对象就原样放 data，核没给就是 null。
 *
 * 数据只经 HTTP 来自核（127.0.0.1:8787）。核现在在 /heart、/brain 上给的是只读 HTML 页，
 * 所以这里会记一条「不是 JSON」并降级成未接入；核哪天在这两个路径上给 JSON，壳不用改就接上。
 */
export interface OrganRoute {
    kind: 'prefix';
    path: string;
    handler: (req: IncomingMessage, res: ServerResponse) => Promise<void>;
}
export interface OrganOptions {
    /** 器官名，同时是核那边的数据端点（heart -> 核的 /heart）。 */
    organ: string;
    /** 核的地址，例如 http://127.0.0.1:8787 */
    upstream: string;
    /** 核卡住多久算读不到（毫秒）。 */
    timeoutMs?: number;
}
export interface OrganPayload {
    organ: string;
    readonly: true;
    /** 拿到一个 JSON 对象才算 true；false 时 data 一定是 null。 */
    ok: boolean;
    data: Record<string, unknown> | null;
    /** 这一次请求里读不到的源，逐条人话；空数组表示这一页没有降级。 */
    errors: string[];
}
/** 每请求重读一次核；任何一环坏掉都只进 errors[]，不抛。 */
export declare function readOrgan(organ: string, upstream: string, timeoutMs?: number): Promise<OrganPayload>;
/** 一条只读的 prefix 路由：/qqagent/<organ>。 */
export declare function organRoute(options: OrganOptions): OrganRoute;
