import type { Job, Page } from './types';

const BASE = '/api/v1';
const KEY = 'mediaflow.token';
const WORKSPACE_KEY = 'mediaflow.workspace';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function stored(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function store(key: string, value: string | null) {
  try {
    if (value) sessionStorage.setItem(key, value);
    else sessionStorage.removeItem(key);
  } catch {
    /* storage unavailable: the value lives for this page only */
  }
}

let token = stored(KEY);
let workspace = stored(WORKSPACE_KEY);
let unauthorized: () => void = () => {};

export const auth = {
  get token() {
    return token;
  },
  /** The workspace every request acts in; null lets the server pick your first one. */
  get workspace() {
    return workspace;
  },
  set(value: string | null) {
    token = value;
    store(KEY, value);
    if (!value) auth.setWorkspace(null); // the next person to sign in may not share it
  },
  setWorkspace(id: string | null) {
    workspace = id;
    store(WORKSPACE_KEY, id);
  },
  onUnauthorized(fn: () => void) {
    unauthorized = fn;
  },
};

/** Auth and tenant headers for every API call. */
export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (workspace) headers['X-Workspace-Id'] = workspace;
  return headers;
}

const OFFLINE = "Can't reach the server. Check your connection and try again.";

function describe(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail) && detail[0]?.msg) {
    return String(detail[0].msg).replace(/^Value error, /, '');
  }
  if (status >= 500) return 'The server ran into a problem. Try again in a moment.';
  return `Something went wrong (${status}).`;
}

function expire() {
  auth.set(null);
  unauthorized();
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers = authHeaders();
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, OFFLINE);
  }
  if (res.status === 204) return undefined as T;
  const data: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith('/auth/login')) expire();
    throw new ApiError(res.status, describe(data, res.status));
  }
  return data as T;
}

/** Load `count` jobs from a paginated endpoint (the API caps a page at 100). */
export async function fetchJobs(path: string, count: number) {
  const items: Job[] = [];
  const sep = path.includes('?') ? '&' : '?';
  while (items.length < count) {
    const limit = Math.min(100, count - items.length);
    const page = await api<Page<Job>>('GET', `${path}${sep}limit=${limit}&offset=${items.length}`);
    items.push(...page.items);
    if (page.items.length < limit) return { items, more: false };
  }
  return { items, more: true };
}

/* Media is fetched through short-lived links (presigned S3 URLs, or links the API signs), so
 * an <img>/<video> can load it directly: streamed, seekable, never held whole in memory. A
 * link is reused until shortly before it expires. */
interface Link { url: string; expiresAt: number }
const media = new Map<string, Promise<Link>>();
const REUSE_MARGIN_MS = 30_000;

export function mediaUrl(jobId: string, { fresh = false } = {}): Promise<string> {
  let hit = fresh ? undefined : media.get(jobId);
  if (hit) {
    const cached = hit;
    hit = cached.then((link) =>
      link.expiresAt - Date.now() > REUSE_MARGIN_MS ? link : newLink(jobId));
  } else {
    hit = newLink(jobId);
  }
  hit.catch(() => media.delete(jobId));
  media.set(jobId, hit);
  return hit.then((link) => link.url);
}

async function newLink(jobId: string): Promise<Link> {
  const out = await api<{ url: string; expires_at: string }>('POST', `/jobs/${jobId}/media-link`);
  return { url: out.url, expiresAt: Date.parse(out.expires_at) };
}

export function clearMedia() {
  media.clear();
}

export interface UploadOutcome {
  status: number;
  jobId: string | null;
}

/** POST /jobs with byte-level progress. The response can take minutes: the API waits for Gemini.
 *  `idempotencyKey` must stay the same across retries of one file, so a retry after a lost
 *  response returns the job the first attempt created instead of making a second one. */
export function uploadFile(
  file: File,
  idempotencyKey: string,
  onProgress: (fraction: number) => void,
  onSent: () => void,
): Promise<UploadOutcome> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${BASE}/jobs`);
    for (const [name, value] of Object.entries(authHeaders())) xhr.setRequestHeader(name, value);
    xhr.setRequestHeader('Idempotency-Key', idempotencyKey);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.upload.onload = onSent;
    xhr.onerror = () => reject(new ApiError(0, OFFLINE));
    xhr.onload = () => {
      let data: { job_id?: string } | null = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        /* non-JSON error body */
      }
      // 502 still carries a job: the upload was accepted but analysis failed.
      if ((xhr.status >= 200 && xhr.status < 300) || (xhr.status === 502 && data?.job_id)) {
        resolve({ status: xhr.status, jobId: data?.job_id ?? null });
        return;
      }
      if (xhr.status === 401) expire();
      reject(new ApiError(xhr.status, describe(data, xhr.status)));
    };
    const form = new FormData();
    form.append('file', file);
    xhr.send(form);
  });
}
