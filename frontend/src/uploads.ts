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
  startedAt: number;
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
  const item = state.items.find((u) => u.id === id);
  if (item?.preview) URL.revokeObjectURL(item.preview);
  set({
    items: state.items.filter((u) => u.id !== id),
    settled: state.settled + (settled ? 1 : 0),
  });
}

/** Uploads live outside any view so they keep going while you look at something else. */
export const uploads = {
  start(files: Iterable<File>) {
    for (const file of files) {
      const id = `u${++seq}`;
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
      uploadFile(
        file,
        (progress) => patch(id, { progress }),
        () => patch(id, { progress: 1, phase: 'sent' }),
      ).then(
        () => drop(id, true),
        (err: unknown) => {
          patch(id, {
            phase: 'error',
            error: err instanceof ApiError ? err.message : 'Upload failed.',
          });
          set({ settled: state.settled + 1 });
        },
      );
    }
  },
  dismiss: (id: string) => drop(id),
  reset() {
    state.items.forEach((u) => u.preview && URL.revokeObjectURL(u.preview));
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
