import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { fakeServer } from '../test/server';
import { ToastProvider } from '../toast';
import { SessionContext } from '../session';
import type { AppNotification, Job, Me } from '../types';
import { JobSheet } from './JobSheet';
import { Notifications } from './Notifications';

const note = (over: Partial<AppNotification> = {}): AppNotification => ({
  id: 'n1', kind: 'draft.submitted', title: 'New draft to review: pier.png',
  body: 'alice sent pier.png for review.', job_id: 'j1', route: 'review',
  created_at: new Date().toISOString(), read: false, ...over,
});

describe('Notifications', () => {
  it('lists, opens and marks notifications read, and turns email copies off', async () => {
    const { calls } = fakeServer({
      'GET /notifications': { body: { items: [note(), note({ id: 'n2', read: true, title: 'Old' })], unread: 1 } },
      'GET /notifications/preferences': { body: { email: true, email_available: true } },
      'POST /notifications/read': { status: 204 },
      'PUT /notifications/preferences': { body: { email: false, email_available: true } },
    });
    const onClose = vi.fn();
    render(<Notifications onClose={onClose} />);
    await userEvent.click(await screen.findByLabelText('Email me as well'));
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ email: false });

    await userEvent.click(screen.getByRole('button', { name: /New draft to review/ }));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ ids: ['n1'] });
    expect(window.location.hash).toBe('#/review?job=j1');
    expect(onClose).toHaveBeenCalled();
  });

  it('says when email is not set up', async () => {
    fakeServer({
      'GET /notifications': { body: { items: [], unread: 0 } },
      'GET /notifications/preferences': { body: { email: true, email_available: false } },
    });
    render(<Notifications onClose={() => {}} />);
    expect(await screen.findByText(/email isn't set up/)).toBeInTheDocument();
    expect(screen.getByText('Nothing yet')).toBeInTheDocument();
  });
});

const ws = { id: 'ws', name: 'MGC', role: 'manager' as const };
const manager: Me = {
  id: 'u2', email: 'max@example.com', role: 'manager', created_at: '2026-01-01T00:00:00Z',
  workspace: ws, workspaces: [ws], platform_admin: false,
};
const job = (over: Partial<Job> = {}): Job => ({
  job_id: 'j1', status: 'done', approval_status: 'pending', draft_version: 1,
  submitter_email: 'alice@example.com', original_filename: 'pier.png', attempts: 1, cached: false,
  error: null, media_kind: 'image', created_at: '2026-01-01T00:00:00Z',
  result: { text: JSON.stringify({ facebook: { caption: 'Hi' } }), kind: 'image', model: 'm', tokens: 0 },
  checks: [], ...over,
});

function renderSheet(j: Job) {
  render(
    <SessionContext.Provider value={{ user: manager, signOut: () => {}, switchWorkspace: () => {} }}>
      <ToastProvider>
        <JobSheet job={j} owned={false} onClose={() => {}} onChanged={() => {}} />
      </ToastProvider>
    </SessionContext.Provider>,
  );
}

describe('Approving', () => {
  it('needs the reviewer to confirm the facts, and sends that confirmation', async () => {
    const { calls } = fakeServer({
      'GET /jobs/j1/comments': { body: [] },
      'PATCH /jobs/j1/approval': { body: job({ approval_status: 'approved' }) },
    });
    renderSheet(job({ checks: [{ level: 'warn', field: 'facebook.caption', message: 'Says "guaranteed"' }] }));
    expect(await screen.findByText('Check before approving')).toBeInTheDocument();
    expect(screen.getByText(/Says "guaranteed"/)).toBeInTheDocument();
    const approve = screen.getByRole('button', { name: 'Approve' });
    expect(approve).toBeDisabled();
    await userEvent.click(screen.getByRole('checkbox', { name: /checked every name, price/ }));
    await userEvent.click(approve);
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ status: 'approved', checked: true });
  });

  it('cannot approve while placeholders are left', async () => {
    fakeServer({ 'GET /jobs/j1/comments': { body: [] } });
    renderSheet(job({ checks: [{ level: 'block', field: 'facebook.caption', message: 'Fill in [NEEDS: price] before approving' }] }));
    expect(await screen.findByText('Fix before approving')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /checked every name/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
  });
});
