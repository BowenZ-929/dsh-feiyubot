import type { IncomingMessage, ServerResponse } from 'node:http';
/**
 * 面板代理：把一个路径前缀下的请求原样搬给核，把核的回包原样搬回来。
 * 这里没有任何判断——不改路径、不改包头、不改包体、不缓存。
 */
export interface ProxyOptions {
    /** 浏览器侧看到的路径前缀；传空串表示原样透传（不剥前缀）。 */
    prefix: string;
    /** 核的地址，例如 http://127.0.0.1:8787 */
    upstream: string;
}
export declare function createProxyHandler(options: ProxyOptions): (req: IncomingMessage, res: ServerResponse) => void;
