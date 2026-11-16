import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { auth } from '../api';
import { fakeServer } from '../test/server';
import type { Me } from '../types';
import { AcceptInvite, readInviteToken } from './Invite';

const TOKEN = 'tok_abcdefghijklmnopqrstuvwxyz0123456789';
const preview = (account_exists: boolean) => ({
  workspace_name: 'Acme', email: 'sam@example.com', role: 'manager',
  expires_at: '2026-12-01T00:00:00Z', account_exists,
});
const ws = { id: 'ws-a', name: 'Acme', role: 'manager' as const };
const me = (email = 'sam@example.com'): Me => ({
  id: 'u1', email, role: 'manager', created_at: '2026-01-01T00:00:00Z',
  workspace: ws, workspaces: [ws], platform_admin: false,
});

function renderInvite(user: Me | null = null) {
  const props = { onJoined: vi.fn(), onSignOut: vi.fn(), onLeave: vi.fn() };
  render(<AcceptInvite token={TOKEN} user={user} {...props} />);
  return props;
}

afterEach(() => {
  auth.set(null);
  window.location.hash = '';
});

describe('readInviteToken', () => {
  it('reads the token from the fragment only', () => {
    window.location.hash = `#/invite/${TOKEN}`;
    expect(readInviteToken()).toBe(TOKEN);
    window.location.hash = '#/library';
    expect(readInviteToken()).toBeNull();
  });
});

describe('AcceptInvite', () => {
  it('lets someone new choose their own password and join', async () => {
    const { calls } = fakeServer({
      'POST /invitations/lookup': { body: preview(false) },
      'POST /invitations/accept': { body: { access_token: 'fresh', workspace: ws } },
      'GET /auth/me': { body: me() },
    });
    const { onJoined } = renderInvite();
    expect(await screen.findByRole('heading', { name: 'Join Acme' })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/^Choose a password/), 'my-own-password-1');
    await userEvent.click(screen.getByRole('button', { name: 'Create account and join' }));

    await vi.waitFor(() => expect(onJoined).toHaveBeenCalled());
    const acceptCall = calls.find((c) => c.path === '/invitations/accept')!;
    expect(acceptCall.body).toEqual({ token: TOKEN, password: 'my-own-password-1' });
    expect(calls.some((c) => c.path === '/auth/login')).toBe(false);
    expect(auth.token).toBe('fresh');
    expect(auth.workspace).toBe('ws-a');
  });

  it('asks an existing account to sign in, then accepts without sending a password', async () => {
    const { calls } = fakeServer({
      'POST /invitations/lookup': { body: preview(true) },
      'POST /auth/login': { body: { access_token: 'signed-in' } },
      'POST /invitations/accept': ({ headers }) => ({
        status: headers.Authorization === 'Bearer signed-in' ? 200 : 409,
        body: { access_token: 'signed-in', workspace: ws },
      }),
      'GET /auth/me': { body: me() },
    });
    const { onJoined } = renderInvite();
    await userEvent.type(await screen.findByLabelText(/^Password/), 'existing-password-1');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in and join' }));

    await vi.waitFor(() => expect(onJoined).toHaveBeenCalled());
    expect(calls.find((c) => c.path === '/auth/login')!.body).toEqual({
      email: 'sam@example.com', password: 'existing-password-1',
    });
    expect(calls.find((c) => c.path === '/invitations/accept')!.body).toEqual({ token: TOKEN });
  });

  it('joins in one click when already signed in as the invited email', async () => {
    fakeServer({
      'POST /invitations/lookup': { body: preview(true) },
      'POST /invitations/accept': { body: { access_token: 'same', workspace: ws } },
      'GET /auth/me': { body: me() },
    });
    const { onJoined } = renderInvite(me());
    expect(screen.queryByLabelText(/^Password/)).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: 'Join workspace' }));
    await vi.waitFor(() => expect(onJoined).toHaveBeenCalled());
  });

  it('tells someone signed in as another account to switch', async () => {
    fakeServer({ 'POST /invitations/lookup': { body: preview(true) } });
    const { onSignOut } = renderInvite(me('other@example.com'));
    expect(await screen.findByRole('status')).toHaveTextContent("You're signed in as other@example.com");
    expect(screen.queryByRole('button', { name: /join/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(onSignOut).toHaveBeenCalled();
  });

  it('explains a used or expired link', async () => {
    fakeServer({
      'POST /invitations/lookup': {
        status: 410, body: { detail: 'This invitation was already used, withdrawn, or has expired.' },
      },
    });
    const { onLeave } = renderInvite();
    expect(await screen.findByRole('heading', { name: "This invitation can't be used" })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('already used');
    await userEvent.click(screen.getByRole('button', { name: 'Go to MediaFlow' }));
    expect(onLeave).toHaveBeenCalled();
  });

  it('keeps a sign-in made for a failed accept from lingering', async () => {
    fakeServer({
      'POST /invitations/lookup': { body: preview(true) },
      'POST /auth/login': { body: { access_token: 'temp' } },
      'POST /invitations/accept': { status: 410, body: { detail: 'This invitation was already used.' } },
    });
    renderInvite();
    await userEvent.type(await screen.findByLabelText(/^Password/), 'existing-password-1');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in and join' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('already used');
    expect(auth.token).toBeNull();
  });
});
