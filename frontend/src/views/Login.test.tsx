import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { fakeServer } from '../test/server';
import type { Me, Meta } from '../types';
import { Login } from './Login';

const closed: Meta = { demo_mode: false, allow_signup: false, demo_password: null, demo_accounts: [] };
const demo: Meta = {
  demo_mode: true,
  allow_signup: true,
  demo_password: 'demo-password-123',
  demo_accounts: [
    { email: 'demo-admin@example.com', role: 'admin' },
    { email: 'demo-member@example.com', role: 'user' },
  ],
};
const me = (role: Me['role'] = 'user'): Me => ({
  id: 'u1', email: 'demo-member@example.com', role, created_at: '2026-01-01T00:00:00Z',
  workspace: { id: 'ws', name: 'Demo Studio', role }, workspaces: [{ id: 'ws', name: 'Demo Studio', role }],
  platform_admin: false,
});

describe('Login', () => {
  it('signs in with email and password', async () => {
    const { calls } = fakeServer({
      'POST /auth/login': { body: { access_token: 'tok' } },
      'GET /auth/me': { body: me() },
    });
    const onSignedIn = vi.fn();
    render(<Login notice="" meta={closed} onSignedIn={onSignedIn} />);
    expect(screen.queryByText('Try a demo account')).not.toBeInTheDocument();
    expect(screen.queryByText('Create a workspace')).not.toBeInTheDocument();

    const submit = screen.getByRole('button', { name: 'Sign in' });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Email'), 'person@example.com');
    await userEvent.type(screen.getByLabelText('Password'), 'a-very-long-password');
    await userEvent.click(submit);

    expect(calls[0]).toMatchObject({
      method: 'POST', path: '/auth/login',
      body: { email: 'person@example.com', password: 'a-very-long-password' },
    });
    expect(calls[1].headers.Authorization).toBe('Bearer tok');
    expect(onSignedIn).toHaveBeenCalledWith(me());
  });

  it('shows the server message for a wrong password without calling it for short ones', async () => {
    const { calls } = fakeServer({
      'POST /auth/login': { status: 401, body: { detail: 'Incorrect email or password' } },
    });
    render(<Login notice="" meta={closed} onSignedIn={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('Email'), 'person@example.com');
    await userEvent.type(screen.getByLabelText('Password'), 'short');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect email or password');
    expect(calls).toHaveLength(0);

    await userEvent.type(screen.getByLabelText('Password'), '-but-now-long-enough');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect email or password');
    expect(calls).toHaveLength(1);
  });

  it('offers one-click demo accounts in demo mode', async () => {
    const { calls } = fakeServer({
      'POST /auth/login': { body: { access_token: 'tok' } },
      'GET /auth/me': { body: me() },
    });
    const onSignedIn = vi.fn();
    render(<Login notice="" meta={demo} onSignedIn={onSignedIn} />);
    await userEvent.click(screen.getByRole('button', { name: 'Continue as Member' }));
    expect(calls[0].body).toEqual({ email: 'demo-member@example.com', password: 'demo-password-123' });
    expect(onSignedIn).toHaveBeenCalled();
  });

  it('creates a workspace when sign-up is open', async () => {
    const { calls } = fakeServer({
      'POST /auth/signup': { status: 201, body: { access_token: 'new' } },
      'GET /auth/me': { body: me('admin') },
    });
    const onSignedIn = vi.fn();
    render(<Login notice="" meta={demo} onSignedIn={onSignedIn} />);
    await userEvent.click(screen.getByRole('button', { name: 'Create a workspace' }));
    expect(screen.getByRole('heading', { name: 'Create your workspace' })).toBeInTheDocument();
    expect(screen.queryByText('Try a demo account')).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/Workspace name/), 'Acme Social');
    await userEvent.type(screen.getByLabelText('Email'), 'founder@example.com');
    await userEvent.type(screen.getByLabelText(/Password/), 'a-very-long-password');
    await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
    expect(calls[0]).toMatchObject({
      path: '/auth/signup',
      body: { email: 'founder@example.com', workspace_name: 'Acme Social' },
    });
    expect(onSignedIn).toHaveBeenCalledWith(me('admin'));
  });
});
