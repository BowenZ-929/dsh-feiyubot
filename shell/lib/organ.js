/** 核只在本机，两秒还不答就当读不到，别让面板一直转圈。 */
const DEFAULT_TIMEOUT_MS = 2000;
function send(res, status, payload, headOnly) {
    const body = Buffer.from(JSON.stringify(payload), 'utf8');
    res.writeHead(status, {
        'content-type': 'application/json; charset=utf-8',
        'content-length': String(body.length),
        'cache-control': 'no-store',
    });
    if (headOnly) {
        res.end();
        return;
    }
    res.end(body);
}
function message(error) {
    if (!(error instanceof Error))
        return String(error);
    // fetch 只报 "fetch failed"，真正的原因（ECONNREFUSED 一类）在 cause 上。
    const cause = error.cause instanceof Error ? error.cause.message : '';
    return cause === '' ? error.message : `${error.message}（${cause}）`;
}
/** 每请求重读一次核；任何一环坏掉都只进 errors[]，不抛。 */
export async function readOrgan(organ, upstream, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const path = '/' + organ;
    const errors = [];
    const failed = () => ({ organ, readonly: true, ok: false, data: null, errors });
    try {
        const response = await fetch(upstream + path, {
            headers: { accept: 'application/json' },
            redirect: 'error',
            signal: AbortSignal.timeout(timeoutMs),
        });
        const text = await response.text();
        if (!response.ok) {
            errors.push(`核 ${path} 回 HTTP ${response.status}`);
            return failed();
        }
        let parsed;
        try {
            parsed = JSON.parse(text);
        }
        catch {
            // 核现在就是走这条：这两个路径上是它的只读 HTML 页，不是数据。
            const type = response.headers.get('content-type') ?? '没有 content-type';
            errors.push(`核 ${path} 回的不是 JSON（${type}）`);
            return failed();
        }
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
            errors.push(`核 ${path} 回的不是单个对象`);
            return failed();
        }
        return { organ, readonly: true, ok: true, data: parsed, errors };
    }
    catch (error) {
        errors.push(`核没有应答（${upstream}${path}）：${message(error)}`);
        return failed();
    }
}
/** 一条只读的 prefix 路由：/qqagent/<organ>。 */
export function organRoute(options) {
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const path = '/qqagent/' + options.organ;
    return {
        kind: 'prefix',
        path,
        async handler(req, res) {
            const headOnly = req.method === 'HEAD';
            if (req.method !== 'GET' && !headOnly) {
                send(res, 405, {
                    ok: false,
                    organ: options.organ,
                    readonly: true,
                    message: `${path} 是只读页：只收 GET/HEAD，${req.method} 一律 405（人不能改心与脑）`,
                }, false);
                return;
            }
            send(res, 200, await readOrgan(options.organ, options.upstream, timeoutMs), headOnly);
        },
    };
}
