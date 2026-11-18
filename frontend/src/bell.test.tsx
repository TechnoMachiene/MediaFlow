import { act, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SessionContext } from './session';
import { fakeServer } from './test/server';
import { ToastProvider } from './toast';
import type { Me, NotificationList } from './types';
import { PageHeader } from './ui';

const ws = { id: 'ws', name: 'MGC', role: 'manager' as const };
const me: Me = {
  id: 'u', email: 'max@example.com', role: 'manager', created_at: '2026-01-01T00:00:00Z',
  workspace: ws, workspaces: [ws], platform_admin: false,
};
const item = (id: string, title: string) => ({
  id, kind: 'draft.submitted', title, body: '', job_id: 'j', route: 'review',
  created_at: new Date().toISOString(), read: false,
});

describe('The notification bell', () => {
  it('turns blue while something is unread and rings, with a message, when something new arrives', async () => {
    let inbox: NotificationList = { items: [item('old', 'Already waiting')], unread: 1 };
    fakeServer({ 'GET /notifications': () => ({ body: inbox }) });
    render(
      <SessionContext.Provider value={{ user: me, signOut: () => {}, switchWorkspace: () => {} }}>
        <ToastProvider><PageHeader title="Library" /></ToastProvider>
      </SessionContext.Provider>,
    );
    const bell = await screen.findByRole('button', { name: 'Notifications, 1 unread' });
    expect(bell).toHaveClass('has-unread');
    // What was already waiting when the page loaded is shown, not announced.
    expect(bell.querySelector('.bell-ring')).toBeNull();
    expect(screen.queryByText('Already waiting')).toBeNull();

    inbox = { items: [item('new', 'New draft to review: pier.png')], unread: 2 };
    await act(async () => { window.dispatchEvent(new Event('mediaflow:notifications-changed')); });
    expect(await screen.findByText('New draft to review: pier.png')).toBeInTheDocument();
    const rung = screen.getByRole('button', { name: 'Notifications, 2 unread' });
    expect(rung.querySelector('.bell-ring')).not.toBeNull();

    inbox = { items: [{ ...item('new', 'x'), read: true }], unread: 0 };
    await act(async () => { window.dispatchEvent(new Event('mediaflow:notifications-changed')); });
    expect(await screen.findByRole('button', { name: 'Notifications' })).not.toHaveClass('has-unread');
  });
});
