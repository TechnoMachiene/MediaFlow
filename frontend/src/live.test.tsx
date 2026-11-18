import { act, render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { readEvents, useLive, type Topic } from './live';

function body(chunks: string[]) {
  const enc = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(c) { chunks.forEach((x) => c.enqueue(enc.encode(x))); c.close(); },
  });
}

describe('readEvents', () => {
  it('reads change messages split anywhere, and skips heartbeats', async () => {
    const seen: Topic[][] = [];
    await readEvents(body([
      'retry: 3000\n\n: ping\n\nevent: change\ndata: {"topi',
      'cs": ["jobs", "audit"]}\n\n: ping\n\nevent: change\ndata: {"topics": ["team"]}\n',
      '\n',
    ]), (t) => seen.push(t));
    expect(seen).toEqual([['jobs', 'audit'], ['team']]);
  });
});

function Probe({ topics, reload }: { topics: Topic[]; reload: () => void }) {
  useLive(topics, reload, 50);
  return null;
}
const change = (topics: string[]) =>
  window.dispatchEvent(new CustomEvent('mediaflow:live', { detail: topics }));

describe('useLive', () => {
  it('reloads once for a burst of changes it cares about, and ignores the rest', () => {
    vi.useFakeTimers();
    const reload = vi.fn();
    render(<Probe topics={['calendar']} reload={reload} />);
    act(() => { change(['jobs']); vi.advanceTimersByTime(100); });
    expect(reload).not.toHaveBeenCalled();
    act(() => { change(['calendar']); change(['calendar', 'jobs']); vi.advanceTimersByTime(100); });
    expect(reload).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
