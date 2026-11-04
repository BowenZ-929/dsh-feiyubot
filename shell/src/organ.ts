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

/** 核只在本机，两秒还不答就当读不到，别让面板一直转圈。 */
const DEFAULT_TIMEOUT_MS = 2000;

export interface OrganPayload {
  organ: string;
  readonly: true;
  /** 拿到一个 JSON 对象才算 true；false 时 data 一定是 null。 */
  ok: boolean;
  data: Record<string, unknown> | null;
  /** 这一次请求里读不到的源，逐条人话；空数组表示这一页没有降级。 */
  errors: string[];
}

function send(res: ServerResponse, status: number, payload: unknown, headOnly: boolean): void {
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

function message(error: unknown): string {
  if (!(error instanceof Error)) return String(error);
  // fetch 只报 "fetch failed"，真正的原因（ECONNREFUSED 一类）在 cause 上。
  const cause = error.cause instanceof Error ? error.cause.message : '';
  return cause === '' ? error.message : `${error.message}（${cause}）`;
}

/** 每请求重读一次核；任何一环坏掉都只进 errors[]，不抛。 */
export async function readOrgan(organ: string, upstream: string, timeoutMs: number = DEFAULT_TIMEOUT_MS): Promise<OrganPayload> {
  const path = '/' + organ;
  const errors: string[] = [];
  const failed = (): OrganPayload => ({ organ, readonly: true, ok: false, data: null, errors });

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
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      // 核现在就是走这条：这两个路径上是它的只读 HTML 页，不是数据。
      const type = response.headers.get('content-type') ?? '没有 content-type';
      errors.push(`核 ${path} 回的不是 JSON（${type}）`);
      return failed();
    }
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      errors.push(`核 ${path} 回的不是单个对象`);
      return failed();
    }
    return { organ, readonly: true, ok: true, data: parsed as Record<string, unknown>, errors };
  } catch (error) {
    errors.push(`核没有应答（${upstream}${path}）：${message(error)}`);
    return failed();
  }
}

/** 一条只读的 prefix 路由：/qqagent/<organ>。 */
export function organRoute(options: OrganOptions): OrganRoute {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const path = '/qqagent/' + options.organ;
  return {
    kind: 'prefix',
    path,
    async handler(req: IncomingMessage, res: ServerResponse): Promise<void> {
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
