import { afterEach, describe, expect, it, vi } from 'vitest';
import { uploads } from './uploads';

/** Minimal XMLHttpRequest stand-in: records each request; the test settles it. */
class FakeXhr {
  static sent: FakeXhr[] = [];
  headers: Record<string, string> = {};
  status = 0;
  responseText = '';
  upload: { onprogress?: unknown; onload?: () => void } = {};
  onload?: () => void;
  onerror?: () => void;
  open() {}
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send() {
    FakeXhr.sent.push(this);
  }
  fail() {
    this.onerror?.();
  }
  reply(status: number, body: unknown) {
    this.status = status;
    this.responseText = JSON.stringify(body);
    this.onload?.();
  }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('uploads', () => {
  afterEach(() => {
    uploads.reset();
    FakeXhr.sent = [];
    vi.unstubAllGlobals();
  });

  it('retries a failed upload with the same Idempotency-Key', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const [id] = uploads.start([new File(['x'], 'clip.mp4'), new File(['y'], 'other.mp4')]);
    const [first, other] = FakeXhr.sent;
    expect(first.headers['Idempotency-Key']).toBeTruthy();
    expect(first.headers['Idempotency-Key']).not.toBe(other.headers['Idempotency-Key']);

    first.fail(); // connection dropped: did the server get it? A keyed retry makes it not matter.
    await settle();
    uploads.retry(id);
    const again = FakeXhr.sent[2];
    expect(again.headers['Idempotency-Key']).toBe(first.headers['Idempotency-Key']);
    again.reply(202, { job_id: 'j1', status: 'queued' });
    other.reply(202, { job_id: 'j2', status: 'queued' });
  });
});
