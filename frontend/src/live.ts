import { useEffect, useRef } from 'react';
import { authHeaders } from './api';

/* Live updates (server: app/api/live.py). One connection per tab streams "this area changed"
 * messages; screens reload what they show when one of their topics arrives. Also refreshes
 * everything when the tab comes back into view, and after a dropped connection reconnects,
 * so nothing missed while away stays stale. */

export type Topic =
  | 'jobs' | 'calendar' | 'publishing' | 'team' | 'audit' | 'workspace' | 'notifications';
const ALL: Topic[] = ['jobs', 'calendar', 'publishing', 'team', 'audit', 'workspace', 'notifications'];
const EVENT = 'mediaflow:live';

function emit(topics: Topic[]) {
  window.dispatchEvent(new CustomEvent<Topic[]>(EVENT, { detail: topics }));
}

/** Parse Server-Sent Events from a fetch body (fetch, not EventSource, so the token travels in
 * a header rather than the URL). Calls `onChange` for each `change` event. */
export async function readEvents(body: ReadableStream<Uint8Array>, onChange: (topics: Topic[]) => void) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let end: number;
    while ((end = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const data = block.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).trim()).join('');
      if (!data) continue; // heartbeat or retry hint
      try {
        const topics = (JSON.parse(data) as { topics?: Topic[] }).topics;
        if (topics?.length) onChange(topics);
      } catch { /* ignore a malformed message */ }
    }
  }
}

/** Keep one live connection open while signed in; returns a stop function. */
export function connectLive(): () => void {
  let stopped = false;
  let controller: AbortController | null = null;
  let delay = 1000;
  let first = true;

  const run = async () => {
    while (!stopped) {
      controller = new AbortController();
      try {
        const res = await fetch('/api/v1/events', { headers: authHeaders(), signal: controller.signal });
        if (res.status === 401 || res.status === 403) return; // signed out or removed: stop
        if (res.ok && res.body) {
          if (!first) emit(ALL); // catch up on anything sent while disconnected
          first = false;
          delay = 1000;
          await readEvents(res.body, emit);
        }
      } catch { /* network drop or abort */ }
      if (stopped) return;
      await new Promise((r) => window.setTimeout(r, delay));
      delay = Math.min(delay * 2, 30_000);
    }
  };
  void run();

  const onVisible = () => { if (!document.hidden) emit(ALL); };
  document.addEventListener('visibilitychange', onVisible);
  return () => {
    stopped = true;
    controller?.abort();
    document.removeEventListener('visibilitychange', onVisible);
  };
}

/** Call `reload` when any of `topics` changes (bursts are coalesced into one reload). */
export function useLive(topics: Topic[], reload: () => unknown, delayMs = 300) {
  const latest = useRef(reload);
  latest.current = reload;
  const key = topics.join(',');
  useEffect(() => {
    const wanted = new Set(key.split(','));
    let timer: number | undefined;
    const onChange = (e: Event) => {
      const changed = (e as CustomEvent<Topic[]>).detail ?? [];
      if (!changed.some((t) => wanted.has(t))) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => void latest.current(), delayMs);
    };
    window.addEventListener(EVENT, onChange);
    return () => {
      window.removeEventListener(EVENT, onChange);
      window.clearTimeout(timer);
    };
  }, [key, delayMs]);
}
