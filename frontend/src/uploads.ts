import { useSyncExternalStore } from 'react';
import { ApiError, uploadFile } from './api';
import type { MediaKind } from './types';

export const ACCEPT: Record<string, MediaKind> = {
  '.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.webp': 'image',
  '.mp4': 'video', '.mov': 'video', '.webm': 'video',
};
export const ACCEPT_ATTR = Object.keys(ACCEPT).join(',');

export interface Upload {
  id: string;
  name: string;
  kind: MediaKind;
  size: number;
  preview: string | null;
  progress: number;
  /** uploading: bytes in flight · sent: server is validating/analyzing · error: rejected */
  phase: 'uploading' | 'sent' | 'error';
  error?: string;
  /** Network or server failure: the same request can be sent again safely. */
  retryable?: boolean;
  startedAt: number;
}

/** What a retry needs. Kept out of `Upload` so the rendered state stays plain data. */
const pending = new Map<string, { file: File; key: string }>();

function newKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${seq}`;
}

interface State {
  items: Upload[];
  /** Bumps whenever a request finishes, so lists know to refresh. */
  settled: number;
}

let state: State = { items: [], settled: 0 };
const listeners = new Set<() => void>();
let seq = 0;

function set(next: Partial<State>) {
  state = { ...state, ...next };
  listeners.forEach((fn) => fn());
}
function patch(id: string, change: Partial<Upload>) {
  set({ items: state.items.map((u) => (u.id === id ? { ...u, ...change } : u)) });
}
function drop(id: string, settled = false) {
  pending.delete(id);
  const item = state.items.find((u) => u.id === id);
  if (item?.preview) URL.revokeObjectURL(item.preview);
  set({
    items: state.items.filter((u) => u.id !== id),
    settled: state.settled + (settled ? 1 : 0),
  });
}

function send(id: string) {
  const job = pending.get(id);
  if (!job) return;
  uploadFile(
    job.file,
    job.key,
    (progress) => patch(id, { progress }),
    () => patch(id, { progress: 1, phase: 'sent' }),
  ).then(
    () => drop(id, true),
    (err: unknown) => {
      const status = err instanceof ApiError ? err.status : 0;
      patch(id, {
        phase: 'error',
        error: err instanceof ApiError ? err.message : 'Upload failed.',
        retryable: status === 0 || status >= 500,
      });
      set({ settled: state.settled + 1 });
    },
  );
}

/** Uploads live outside any view so they keep going while you look at something else. */
export const uploads = {
  /** Returns the ids of the uploads it started (or rejected), in order. */
  start(files: Iterable<File>): string[] {
    const ids: string[] = [];
    for (const file of files) {
      const id = `u${++seq}`;
      ids.push(id);
      const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
      const kind = ACCEPT[ext];
      const base: Upload = {
        id, name: file.name, size: file.size, kind: kind ?? 'image',
        preview: kind === 'image' ? URL.createObjectURL(file) : null,
        progress: 0, phase: 'uploading', startedAt: Date.now(),
      };
      if (!kind) {
        set({
          items: [
            { ...base, phase: 'error', error: 'Not supported. Use JPG, PNG, WebP, MP4, MOV, or WebM.' },
            ...state.items,
          ],
        });
        continue;
      }
      set({ items: [base, ...state.items] });
      pending.set(id, { file, key: newKey() });
      send(id);
    }
    return ids;
  },
  /** Send a failed upload again with the same Idempotency-Key: if the first attempt did reach
   *  the server, this returns that job rather than creating a duplicate. */
  retry(id: string) {
    if (!pending.has(id)) return;
    patch(id, { phase: 'uploading', progress: 0, error: undefined, retryable: undefined });
    send(id);
  },
  dismiss: (id: string) => drop(id),
  reset() {
    state.items.forEach((u) => u.preview && URL.revokeObjectURL(u.preview));
    pending.clear();
    set({ items: [] });
  },
};

export function useUploads(): State {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    () => state,
  );
}
