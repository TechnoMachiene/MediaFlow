import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { clearMedia, mediaUrl } from '../api';
import { SessionContext } from '../session';
import { fakeServer } from '../test/server';
import { ToastProvider } from '../toast';
import type { Me, RetentionView } from '../types';
import { Retention } from './Settings';

const ws = { id: 'ws-a', name: 'Acme', role: 'admin' as const };
const me: Me = {
  id: 'u1', email: 'anna@example.com', role: 'admin', created_at: '2026-01-01T00:00:00Z',
  workspace: ws, workspaces: [ws], platform_admin: false,
};
const none = { originals_expired: 0, originals_published: 0, rejected_deleted: 0 };
const view = (over: Partial<RetentionView> = {}): RetentionView => ({
  originals_days: null, delete_after_publish: false, rejected_days: null,
  effective_originals_days: 30, default_days: 30, max_days: 0, preview: none, ...over,
});

function renderRetention() {
  render(
    <ToastProvider>
      <SessionContext.Provider value={{ user: me, signOut: () => {}, switchWorkspace: () => {} }}>
        <Retention />
      </SessionContext.Provider>
    </ToastProvider>,
  );
}

describe('Retention settings', () => {
  it('previews what a change would delete before it is saved, then saves it', async () => {
    const { calls } = fakeServer({
      'GET /workspaces/ws-a/retention': { body: view() },
      'POST /workspaces/ws-a/retention/preview': ({ body }) => ({
        body: (body as { originals_days: number }).originals_days === 7
          ? { ...none, originals_expired: 3 } : none,
      }),
      'PUT /workspaces/ws-a/retention': { body: view({ originals_days: 7, effective_originals_days: 7,
        preview: { ...none, originals_expired: 3 } }) },
    });
    renderRetention();
    expect(await screen.findByText('Nothing would be deleted right now.')).toBeInTheDocument();
    const save = screen.getByRole('button', { name: 'Save policy' });
    expect(save).toBeDisabled();

    const days = screen.getByLabelText('Days to keep originals');
    await userEvent.clear(days);
    await userEvent.type(days, '7');
    expect(await screen.findByText(/If saved, the next run would delete 3 original/)).toBeInTheDocument();
    await userEvent.click(save);
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({
      originals_days: 7, delete_after_publish: false, rejected_days: null,
    });
    expect(await screen.findByText(/The next run will delete 3 original/)).toBeInTheDocument();
  });

  it('cannot keep forever when the installation sets a ceiling', async () => {
    fakeServer({ 'GET /workspaces/ws-a/retention': { body: view({ max_days: 90 }) } });
    renderRetention();
    const forever = await screen.findByRole('radio', { name: /Forever/ });
    expect(forever).toBeDisabled();
    expect(screen.getByText(/at most 90 days/)).toBeInTheDocument();
  });
});

describe('mediaUrl', () => {
  it('reuses a link until shortly before it expires', async () => {
    clearMedia();
    let n = 0;
    const { calls } = fakeServer({
      'POST /jobs/j1/media-link': () => ({
        body: { url: `/api/v1/media/t${++n}`, kind: 'signed',
          expires_at: new Date(Date.now() + (n === 1 ? 300_000 : 10_000)).toISOString() },
      }),
    });
    expect(await mediaUrl('j1')).toBe('/api/v1/media/t1');
    expect(await mediaUrl('j1')).toBe('/api/v1/media/t1'); // still fresh: no new request
    expect(await mediaUrl('j1', { fresh: true })).toBe('/api/v1/media/t2');
    expect(await mediaUrl('j1')).toBe('/api/v1/media/t3'); // t2 expires within 30 s
    expect(calls.filter((c) => c.path === '/jobs/j1/media-link')).toHaveLength(3);
  });
});
