import { request as httpRequest } from 'node:http';
/** 核卡住不答时，面板上宁可看到一句错误，也别一直转圈。 */
const ANSWER_TIMEOUT_MS = 15000;
function fail(res, message) {
    if (res.headersSent) {
        res.destroy();
        return;
    }
    const body = JSON.stringify({ ok: false, message });
    res.writeHead(502, {
        'content-type': 'application/json; charset=utf-8',
        'content-length': Buffer.byteLength(body),
        'cache-control': 'no-store',
    });
    res.end(body);
}
export function createProxyHandler(options) {
    const target = new URL(options.upstream);
    const prefixLength = options.prefix.length;
    return function proxy(req, res) {
        if (target.protocol !== 'http:') {
            fail(res, `核的地址只能是 http：${options.upstream}`);
            return;
        }
        const raw = req.url ?? '/';
        const path = prefixLength > 0 && raw.startsWith(options.prefix)
            ? '/' + raw.slice(prefixLength).replace(/^\//, '')
            : raw;
        const upstreamReq = httpRequest({
            hostname: target.hostname,
            port: target.port || 80,
            method: req.method,
            path,
            headers: { ...req.headers, host: target.host },
        }, (upstreamRes) => {
            res.writeHead(upstreamRes.statusCode ?? 502, upstreamRes.headers);
            upstreamRes.pipe(res);
            upstreamRes.on('error', () => res.destroy());
        });
        upstreamReq.setTimeout(ANSWER_TIMEOUT_MS, () => {
            upstreamReq.destroy(new Error(`超过 ${ANSWER_TIMEOUT_MS} 毫秒没有应答`));
        });
        upstreamReq.on('error', (error) => {
            fail(res, `核没有应答（${options.upstream}）：${error.message}`);
        });
        res.on('close', () => {
            if (!res.writableEnded)
                upstreamReq.destroy();
        });
        req.pipe(upstreamReq);
    };
}
