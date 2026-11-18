import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe as suite, expect, it } from 'vitest';
import { SessionContext } from '../session';
import { fakeServer } from '../test/server';
import type { AuditEvent, Me } from '../types';
import { Audit, describe } from './Audit';

const event = (over: Partial<AuditEvent>): AuditEvent => ({
  id: 1, created_at: new Date().toISOString(), actor_id: 'u1', actor_email: 'anna@example.com',
  action: 'member.role_changed', target_type: 'user', target_id: 'u2',
  target_label: 'alice@example.com', before: null, after: null, request_id: 'r1', ...over,
});

const ws = { id: 'ws-a', name: 'Acme', role: 'admin' as const };
const me: Me = {
  id: 'u1', email: 'anna@example.com', role: 'admin', created_at: '2026-01-01T00:00:00Z',
  workspace: ws, workspaces: [ws], platform_admin: false,
};

function renderAudit() {
  render(
    <SessionContext.Provider value={{ user: me, signOut: () => {}, switchWorkspace: () => {} }}>
      <Audit />
    </SessionContext.Provider>,
  );
}

suite('describe', () => {
  it('turns events into sentences', () => {
    expect(describe(event({ before: { role: 'user' }, after: { role: 'manager' } })))
      .toBe('anna@example.com changed alice@example.com from Member to Manager');
    expect(describe(event({
      action: 'job.approved', target_label: 'cat.png', after: { owner: 'alice@example.com' },
    }))).toBe('anna@example.com approved cat.png by alice@example.com');
    expect(describe(event({
      action: 'member.removed', before: { role: 'user' }, after: { jobs_deleted: 3 },
    }))).toBe('anna@example.com removed alice@example.com (Member) and 3 of their jobs');
    expect(describe(event({
      action: 'draft.submitted', target_label: 'cat.png',
      before: { approval_status: 'with_uploader' }, after: { approval_status: 'pending', version: 2 },
    }))).toBe('anna@example.com sent version 2 of cat.png for review');
    expect(describe(event({
      action: 'draft.edited', target_label: 'cat.png',
      before: { approval_status: 'rejected' }, after: { approval_status: 'with_uploader', version: 3 },
    }))).toBe('anna@example.com saved version 3 of cat.png, taking it back to the uploader');
    expect(describe(event({ action: 'member.password_reset', actor_email: 'system:cli', actor_id: null })))
      .toBe('The admin command reset the password of alice@example.com');
  });
});

suite('Audit', () => {
  it('filters by category and pages back through older events', async () => {
    const { calls } = fakeServer({
      'GET /audit-events': ({ path }) => {
        const query = new URLSearchParams(path.split('?')[1]);
        if (query.get('before') === '7') {
          return { body: { items: [event({ id: 6, action: 'invitation.revoked' })], next_before: null } };
        }
        return {
          body: {
            items: [event({ id: 7, action: 'invitation.created', after: { role: 'manager' } })],
            next_before: 7,
          },
        };
      },
    });
    renderAudit();
    expect(await screen.findByText('anna@example.com invited alice@example.com as Manager'))
      .toBeInTheDocument();
    await userEvent.click(screen.getByRole('radio', { name: 'Invites' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Show older' }));
    expect(await screen.findByText('anna@example.com withdrew the invitation for alice@example.com'))
      .toBeInTheDocument();
    const paths = calls.map((c) => c.path).filter((p) => p.startsWith('/audit-events'));
    expect(paths.some((p) => p.includes('category=invitation') && p.includes('before=7'))).toBe(true);
    expect(screen.queryByRole('button', { name: 'Show older' })).not.toBeInTheDocument();
  });

  it('says so when nothing has happened yet', async () => {
    fakeServer({ 'GET /audit-events': { body: { items: [], next_before: null } } });
    renderAudit();
    expect(await screen.findByRole('heading', { name: 'Nothing recorded yet' })).toBeInTheDocument();
  });
});
