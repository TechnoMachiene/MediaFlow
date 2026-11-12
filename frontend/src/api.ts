import type { Job, Page } from './types';

const BASE = '/api/v1';
const KEY = 'mediaflow.token';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function stored(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

let token = stored();
let unauthorized: () => void = () => {};

export const auth = {
  get token() {
    return token;
  },
  set(value: string | null) {
    token = value;
    try {
      if (value) sessionStorage.setItem(KEY, value);
      else sessionStorage.removeItem(KEY);
    } catch {
      /* storage unavailable: the token lives for this page only */
    }
  },
  onUnauthorized(fn: () => void) {
    unauthorized = fn;
  },
};

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
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
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
    if (res.status === 401 && path !== '/auth/login') expire();
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

/* Media is behind the bearer token, so it can't be an <img src>. Fetch once, keep a blob URL. */
const media = new Map<string, Promise<string>>();

export function mediaUrl(jobId: string): Promise<string> {
  let hit = media.get(jobId);
  if (!hit) {
    hit = fetch(`${BASE}/jobs/${jobId}/media`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    }).then(async (res) => {
      if (res.status === 401) expire();
      if (!res.ok) throw new ApiError(res.status, 'Media unavailable');
      return URL.createObjectURL(await res.blob());
    });
    hit.catch(() => media.delete(jobId));
    media.set(jobId, hit);
  }
  return hit;
}

export function clearMedia() {
  for (const entry of media.values()) entry.then(URL.revokeObjectURL, () => {});
  media.clear();
}

export interface UploadOutcome {
  status: number;
  jobId: string | null;
}

/** POST /jobs with byte-level progress. The response can take minutes: the API waits for Gemini. */
export function uploadFile(
  file: File,
  onProgress: (fraction: number) => void,
  onSent: () => void,
): Promise<UploadOutcome> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${BASE}/jobs`);
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
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
