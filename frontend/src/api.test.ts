import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, auth, authHeaders, fetchJobs } from './api';
import { fakeServer } from './test/server';

beforeEach(() => {
  auth.set(null);
});

describe('api client', () => {
  it('sends the token and the selected workspace on every call', async () => {
    const { calls } = fakeServer({ 'GET /jobs/mine': { body: { items: [] } } });
    auth.set('tok-1');
    auth.setWorkspace('ws-b');
    await api('GET', '/jobs/mine');
    expect(calls[0].headers).toMatchObject({ Authorization: 'Bearer tok-1', 'X-Workspace-Id': 'ws-b' });
    expect(sessionStorage.getItem('mediaflow.workspace')).toBe('ws-b');
  });

  it('forgets the workspace on sign-out so the next person starts clean', () => {
    auth.set('tok-1');
    auth.setWorkspace('ws-a');
    auth.set(null);
    expect(auth.workspace).toBeNull();
    expect(authHeaders()).toEqual({});
  });

  it('expires the session on 401, except for a failed sign-in', async () => {
    const expired = vi.fn();
    auth.onUnauthorized(expired);
    fakeServer({
      'GET /auth/me': { status: 401, body: { detail: 'Invalid or expired token' } },
      'POST /auth/login': { status: 401, body: { detail: 'Incorrect email or password' } },
    });
    auth.set('stale');
    await expect(api('POST', '/auth/login', { email: 'a', password: 'b' })).rejects.toThrow(
      'Incorrect email or password',
    );
    expect(expired).not.toHaveBeenCalled();
    await expect(api('GET', '/auth/me')).rejects.toBeInstanceOf(ApiError);
    expect(expired).toHaveBeenCalledOnce();
    expect(auth.token).toBeNull();
  });

  it('turns server errors into sentences people can act on', async () => {
    fakeServer({
      'POST /users': { status: 422, body: { detail: [{ msg: 'Value error, Workspace name cannot be blank' }] } },
      'GET /stats': { status: 503, body: null },
      'GET /jobs': { status: 409, body: {} },
    });
    await expect(api('POST', '/users', {})).rejects.toThrow('Workspace name cannot be blank');
    await expect(api('GET', '/stats')).rejects.toThrow('The server ran into a problem');
    await expect(api('GET', '/jobs')).rejects.toThrow('Something went wrong (409).');
  });

  it('reports being offline distinctly', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(api('GET', '/jobs')).rejects.toMatchObject({ status: 0 });
  });

  it('pages through long job lists 100 at a time', async () => {
    const page = (n: number) => Array.from({ length: n }, (_, i) => ({ job_id: String(i) }));
    const { calls } = fakeServer({
      'GET /jobs': ({ path }) => ({
        body: { items: page(path.includes('offset=0') ? 100 : 30), limit: 100, offset: 0 },
      }),
    });
    const out = await fetchJobs('/jobs?kind=image', 250);
    expect(out.items).toHaveLength(130);
    expect(out.more).toBe(false);
    expect(calls.map((c) => c.path)).toEqual([
      '/jobs?kind=image&limit=100&offset=0',
      '/jobs?kind=image&limit=100&offset=100',
    ]);
  });
});
