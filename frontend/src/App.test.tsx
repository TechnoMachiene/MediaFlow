import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App, { WorkspaceSwitcher } from './App';
import { auth } from './api';
import { fakeServer } from './test/server';
import type { Me } from './types';

const ws = (id: string, name: string, role: Me['role']) => ({ id, name, role });
const me = (current: 'a' | 'b'): Me => {
  const all = [ws('a', 'Acme', 'user'), ws('b', 'Beta', 'admin')];
  const workspace = all.find((w) => w.id === current)!;
  return {
    id: 'u1', email: 'sam@example.com', role: workspace.role, created_at: '2026-01-01T00:00:00Z',
    workspace, workspaces: all, platform_admin: false,
  };
};

describe('WorkspaceSwitcher', () => {
  it('just names the workspace when there is only one', () => {
    const single = { ...me('a'), workspaces: [ws('a', 'Acme', 'user')] };
    render(<WorkspaceSwitcher user={single} onSwitch={vi.fn()} />);
    expect(screen.getByText('Acme')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('lists every workspace with your role in it', async () => {
    const onSwitch = vi.fn();
    render(<WorkspaceSwitcher user={me('a')} onSwitch={onSwitch} />);
    const select = screen.getByRole('combobox', { name: 'Workspace' });
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Acme · Member', 'Beta · Admin',
    ]);
    await userEvent.selectOptions(select, 'b');
    expect(onSwitch).toHaveBeenCalledWith('b');
  });
});

describe('App', () => {
  it('shows the demo banner and lets a visitor in with one click', async () => {
    fakeServer({
      'GET /meta': {
        body: {
          demo_mode: true, allow_signup: false, demo_password: 'demo-password-123',
          demo_accounts: [{ email: 'sam@example.com', role: 'user' }],
        },
      },
      'POST /auth/login': { body: { access_token: 'tok' } },
      'GET /auth/me': { body: me('a') },
    });
    render(<App />);
    expect(await screen.findByRole('note')).toHaveTextContent('Demo mode');
    await userEvent.click(await screen.findByRole('button', { name: 'Continue as Member' }));
    expect(await screen.findByRole('heading', { name: 'Library' })).toBeInTheDocument();
    // A member sees no review, team or activity screens.
    const nav = screen.getAllByRole('navigation', { name: 'Main' })[0];
    expect(within(nav).getAllByRole('link').map((l) => l.textContent)).toEqual(['Library', 'Calendar']);
  });

  it('switching workspace re-reads your role and scopes every later request', async () => {
    auth.set('tok');
    const { calls } = fakeServer({
      'GET /meta': { body: { demo_mode: false, allow_signup: false, demo_password: null, demo_accounts: [] } },
      'GET /auth/me': ({ headers }) => ({ body: me(headers['X-Workspace-Id'] === 'b' ? 'b' : 'a') }),
      'GET /stats': { body: { ready: 0, processing: 0, deferred: 0, dead: 0, gemini_calls_today: null } },
    });
    render(<App />);
    await screen.findByRole('heading', { name: 'Library' });
    const select = screen.getAllByRole('combobox', { name: 'Workspace' })[0];
    await userEvent.selectOptions(select, 'b');

    const nav = screen.getAllByRole('navigation', { name: 'Main' })[0];
    expect(await within(nav).findByRole('link', { name: 'Activity' })).toBeInTheDocument();
    expect(auth.workspace).toBe('b');
    const afterSwitch = calls.slice(calls.findIndex((c) => c.headers['X-Workspace-Id'] === 'b'));
    expect(afterSwitch.length).toBeGreaterThan(1);
    expect(afterSwitch.every((c) => c.path === '/meta' || c.headers['X-Workspace-Id'] === 'b')).toBe(true);
  });
});
