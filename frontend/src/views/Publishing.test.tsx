import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { fakeServer } from '../test/server';
import { ToastProvider } from '../toast';
import type { Connection, Publication } from '../types';
import { PostPublishing, PublishingAuth } from './Publishing';

const conn = (over: Partial<Connection> = {}): Connection => ({
  id: 'c1', provider: 'mock', provider_name: 'Sandbox', sandbox: true, channel: 'instagram',
  account_name: 'Sandbox ab12', status: 'active', options: { behavior: 'succeed' }, last_error: null,
  connected_by_email: 'anna@example.com', token_expires_at: null, created_at: '2026-01-01T00:00:00Z',
  ...over,
});
const pub = (over: Partial<Publication> = {}): Publication => ({
  id: 'p1', scheduled_post_id: 'post1', connection_id: 'c1', account_name: 'Sandbox ab12',
  provider: 'mock', channel: 'instagram', sandbox: true, status: 'queued', attempts: 0,
  next_attempt_at: null, error_code: null, last_error: null, external_id: null, external_url: null,
  published_at: null, requested_by_email: 'alice@example.com', created_at: '2026-01-01T00:00:00Z',
  ...over,
});

const wrap = (ui: React.ReactElement) => render(<ToastProvider>{ui}</ToastProvider>);

describe('PostPublishing', () => {
  it('adds an account to a calendar entry', async () => {
    const { calls } = fakeServer({
      'GET /scheduled-posts/post1/publications': { body: [] },
      'GET /publishing/connections': { body: [conn(), conn({ id: 'c2', status: 'needs_reauth' })] },
      'POST /scheduled-posts/post1/publications': { status: 201, body: pub() },
    });
    wrap(<PostPublishing postId="post1" active />);
    const select = await screen.findByLabelText('Also publish to');
    expect(within(select).getAllByRole('option')).toHaveLength(1); // accounts needing reauth are hidden
    await userEvent.click(screen.getByRole('button', { name: 'Publish at this time' }));
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ connection_id: 'c1' });
  });

  it('shows delivery state and offers the right next step', async () => {
    const { calls } = fakeServer({
      'GET /scheduled-posts/post1/publications': {
        body: [
          pub({ id: 'p1', status: 'failed', error_code: 'rejected', last_error: 'Sandbox: refused' }),
          pub({ id: 'p2', connection_id: 'c2', account_name: 'Sandbox cd34', status: 'unknown',
            last_error: 'Check the account, then retry or cancel.' }),
        ],
      },
      'GET /publishing/connections': { body: [] },
      'POST /publications/p1/retry': { body: pub({ status: 'queued' }) },
    });
    wrap(<PostPublishing postId="post1" active />);
    expect(await screen.findByText('Failed')).toBeInTheDocument();
    expect(screen.getByText('Sandbox: refused')).toBeInTheDocument();
    expect(screen.getByText('Needs a check')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'I checked: send again' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await vi.waitFor(() => expect(calls.some((c) => c.path === '/publications/p1/retry')).toBe(true));
  });

  it('stays out of the way when publishing is off', async () => {
    fakeServer({ 'GET /scheduled-posts/post1/publications': { status: 404, body: {} } });
    const { container } = wrap(<PostPublishing postId="post1" active />);
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector('.publishing')).toBeNull();
  });
});

describe('PublishingAuth', () => {
  it('runs the sandbox consent and finishes the connection', async () => {
    const { calls } = fakeServer({
      'POST /publishing/sandbox/authorize': { body: { code: 'sbx.code' } },
      'POST /publishing/oauth/callback': { body: conn() },
    });
    const onDone = vi.fn();
    const params = new URLSearchParams({ state: 's'.repeat(30), challenge: 'c'.repeat(43) });
    wrap(<PublishingAuth kind="sandbox" params={params} onDone={onDone} />);
    await userEvent.click(screen.getByRole('button', { name: 'Allow' }));
    await vi.waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(calls.map((c) => c.path)).toEqual(['/publishing/sandbox/authorize', '/publishing/oauth/callback']);
    expect(calls[1].body).toEqual({ state: 's'.repeat(30), code: 'sbx.code' });
  });

  it('sends a denial as an error, not a code', async () => {
    const { calls } = fakeServer({
      'POST /publishing/sandbox/authorize': { body: { code: 'access_denied' } },
      'POST /publishing/oauth/callback': { status: 400, body: { detail: 'Access was not granted' } },
    });
    const params = new URLSearchParams({ state: 's'.repeat(30), challenge: 'c'.repeat(43) });
    wrap(<PublishingAuth kind="sandbox" params={params} onDone={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Deny' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Access was not granted');
    expect(calls[1].body).toEqual({ state: 's'.repeat(30), error: 'access_denied' });
  });

  it('finishes a real provider redirect from the callback URL', async () => {
    const { calls } = fakeServer({ 'POST /publishing/oauth/callback': { body: conn() } });
    const onDone = vi.fn();
    const params = new URLSearchParams({ state: 's'.repeat(30), code: 'real-code' });
    wrap(<PublishingAuth kind="callback" params={params} onDone={onDone} />);
    await vi.waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(calls[0].body).toEqual({ state: 's'.repeat(30), code: 'real-code' });
  });
});
