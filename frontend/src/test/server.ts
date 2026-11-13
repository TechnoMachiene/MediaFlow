import { vi } from 'vitest';

export interface Call {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

type Handler = (call: Call) => { status?: number; body?: unknown } | undefined;

/** A fetch stand-in routed by "METHOD /path" (path without /api/v1, query ignored). */
export function fakeServer(routes: Record<string, Handler | { status?: number; body?: unknown }>) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), 'http://localhost');
    const path = url.pathname.replace(/^\/api\/v1/, '');
    const method = (init.method ?? 'GET').toUpperCase();
    const call: Call = {
      method,
      path: path + url.search,
      headers: Object.fromEntries(Object.entries((init.headers ?? {}) as Record<string, string>)),
      body: typeof init.body === 'string' ? JSON.parse(init.body) : init.body,
    };
    calls.push(call);
    const route = routes[`${method} ${path}`];
    const out = typeof route === 'function' ? route(call) : route;
    if (!out) return new Response(JSON.stringify({ items: [], limit: 0, offset: 0 }), { status: 200 });
    const status = out.status ?? 200;
    return new Response(status === 204 ? null : JSON.stringify(out.body ?? null), { status });
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls, fetchMock };
}
