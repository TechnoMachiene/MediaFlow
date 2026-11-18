import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { SessionContext } from '../session';
import { fakeServer } from '../test/server';
import { ToastProvider } from '../toast';
import type { Delivery, Me, Post } from '../types';
import { Calendar } from './Calendar';

const ws = { id: 'ws', name: 'MGC', role: 'user' as const };
const me: Me = {
  id: 'u', email: 'alice@example.com', role: 'user', created_at: '2026-01-01T00:00:00Z',
  workspace: ws, workspaces: [ws], platform_admin: false,
};
const today = new Date().toISOString().slice(0, 10);
const post = (id: string, delivery: Delivery, url: string | null = null): Post => ({
  id, job_id: `j-${id}`, original_filename: `${id}.png`, media_kind: 'image',
  status: delivery === 'cancelled' ? 'cancelled' : 'scheduled',
  scheduled_at: `${today}T12:00:00Z`, timezone: 'UTC', local_date: today,
  content: JSON.stringify({ facebook: { caption: `Caption ${id}` } }),
  created_at: '2026-01-01T00:00:00Z', delivery, published_url: url,
});

describe('Calendar', () => {
  it('tells posted, failed and past posts apart from planned ones', async () => {
    const posts = [post('a', 'published', 'https://www.facebook.com/1_2'), post('b', 'failed'),
      post('c', 'past'), post('d', 'upcoming')];
    fakeServer({
      'GET /calendar': () => ({ body: { timezone: 'UTC', days: [{ date: today, posts }] } }),
      'GET /scheduled-posts/a/publications': { body: [] },
      'GET /publishing/connections': { body: [] },
    });
    localStorage.setItem('mediaflow.tz', 'UTC');
    render(
      <SessionContext.Provider value={{ user: me, signOut: () => {}, switchWorkspace: () => {} }}>
        <ToastProvider><Calendar /></ToastProvider>
      </SessionContext.Provider>,
    );
    const rows = await screen.findAllByRole('button', { name: /Caption/ });
    const badges = rows.map((r) => within(r).getByText(/Posted|Didn't post|Time passed|Planned/).textContent);
    expect(badges).toEqual(['Posted', "Didn't post", 'Time passed', 'Planned']);
    expect(within(screen.getByRole('list', { name: 'Key' })).getAllByRole('listitem')).toHaveLength(4);

    await userEvent.click(rows[0]);
    const link = await screen.findByRole('link', { name: 'View the post' });
    expect(link).toHaveAttribute('href', 'https://www.facebook.com/1_2');
  });
});
