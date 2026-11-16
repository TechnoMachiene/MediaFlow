import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { fakeServer } from '../test/server';
import { ToastProvider } from '../toast';
import type { DraftComment, DraftVersion, Job } from '../types';
import { DraftPanel } from './Draft';

const V1 = JSON.stringify({ instagram: { caption: 'Golden hour', hashtags: ['#sunset'] } });
const V2 = JSON.stringify({ instagram: { caption: 'Golden hour on the pier', hashtags: ['#sunset'] } });

const job = (over: Partial<Job> = {}): Job => ({
  job_id: 'j1', status: 'done', approval_status: 'pending', draft_version: 1,
  submitter_email: null, original_filename: 'pier.png', attempts: 1, cached: false, error: null,
  result: { text: V1, kind: 'image', model: 'm', tokens: 0 }, media_kind: 'image',
  created_at: '2026-01-01T00:00:00Z', ...over,
});
const version = (number: number, text: string, over: Partial<DraftVersion> = {}): DraftVersion => ({
  number, text, source: number === 1 ? 'ai' : 'edit', restored_from: null, note: null,
  author_email: number === 1 ? null : 'alice@example.com', created_at: new Date().toISOString(),
  current: false, ...over,
});
const comment = (over: Partial<DraftComment>): DraftComment => ({
  id: 'c1', parent_id: null, version: 1, field: null, kind: 'comment', body: 'Hi',
  author_email: 'max@example.com', created_at: new Date().toISOString(), resolved_at: null,
  resolved_by_email: null, ...over,
});

function renderPanel(j: Job, canEdit = true) {
  const onChanged = vi.fn();
  render(<ToastProvider><DraftPanel job={j} canEdit={canEdit} onChanged={onChanged} /></ToastProvider>);
  return onChanged;
}

describe('DraftPanel', () => {
  it('edits a field and saves it as a new version based on the current one', async () => {
    const { calls } = fakeServer({
      'GET /jobs/j1/comments': { body: [] },
      'POST /jobs/j1/versions': {
        status: 201,
        body: { version: version(2, V2, { current: true }), approval_status: 'pending', returned_to_review: true },
      },
      'GET /jobs/j1': { body: job({ draft_version: 2 }) },
    });
    const onChanged = renderPanel(job({ approval_status: 'approved' }));
    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByText(/Saving a change sends it back for review/)).toBeInTheDocument();
    const save = screen.getByRole('button', { name: 'Save version' });
    expect(save).toBeDisabled(); // nothing changed yet

    const caption = screen.getByLabelText('Caption');
    await userEvent.clear(caption);
    await userEvent.type(caption, 'Golden hour on the pier');
    await userEvent.type(screen.getByLabelText(/What changed/), 'More specific');
    await userEvent.click(save);

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalled());
    const body = calls.find((c) => c.method === 'POST')!.body as Record<string, unknown>;
    expect(body.base_version).toBe(1);
    expect(body.note).toBe('More specific');
    expect(JSON.parse(body.text as string)).toEqual(JSON.parse(V2));
  });

  it('explains a conflicting save and offers to reload', async () => {
    fakeServer({
      'GET /jobs/j1/comments': { body: [] },
      'POST /jobs/j1/versions': {
        status: 409,
        body: { detail: 'Someone saved version 2 while you were editing. Reload to see it, then edit again.' },
      },
      'GET /jobs/j1': { body: job({ draft_version: 2 }) },
    });
    const onChanged = renderPanel(job());
    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    await userEvent.type(screen.getByLabelText('Caption'), '!');
    await userEvent.click(screen.getByRole('button', { name: 'Save version' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('while you were editing');
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('shows what changed between versions and restores an older one', async () => {
    const { calls } = fakeServer({
      'GET /jobs/j1/comments': { body: [] },
      'GET /jobs/j1/versions': {
        body: [version(2, V2, { current: true, note: 'More specific' }), version(1, V1)],
      },
      'POST /jobs/j1/versions': {
        status: 201,
        body: { version: version(3, V1, { source: 'restore', restored_from: 1, current: true }),
          approval_status: 'pending', returned_to_review: false },
      },
      'GET /jobs/j1': { body: job({ draft_version: 3 }) },
    });
    renderPanel(job({ draft_version: 2, result: { text: V2, kind: 'image', model: 'm', tokens: 0 } }));
    await userEvent.click(screen.getByRole('radio', { name: 'History' }));

    const changes = await screen.findByRole('list', { name: 'Changes from version 1' });
    expect(within(changes).getByText('Instagram · Caption')).toBeInTheDocument();
    expect(within(changes).getByText('Golden hour').tagName).toBe('DEL');
    expect(within(changes).getByText('Golden hour on the pier').tagName).toBe('INS');
    expect(screen.getByText('Version 2 · More specific')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Restore version 2' })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Version 1 · Original draft/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Restore version 1' }));
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ base_version: 2, restore: 1 });
  });

  it('starts a comment from a field and shows open threads', async () => {
    const { calls } = fakeServer({
      'GET /jobs/j1/comments': {
        body: [
          comment({ id: 'c1', field: 'instagram.caption', body: 'Too short' }),
          comment({ id: 'c2', parent_id: 'c1', body: 'Agreed', author_email: 'alice@example.com' }),
          comment({ id: 'c3', kind: 'rejected', body: 'Wrong product', resolved_at: '2026-01-02T00:00:00Z',
            resolved_by_email: 'alice@example.com' }),
        ],
      },
      'POST /jobs/j1/comments': { status: 201, body: comment({ id: 'c4' }) },
    });
    renderPanel(job());
    expect(await screen.findByRole('radio', { name: 'Comments (1)' })).toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: '1 comment' }));

    const open = screen.getByRole('list', { name: 'Open comments' });
    expect(within(open).getByText('Too short')).toBeInTheDocument();
    expect(within(open).getByText('Agreed')).toBeInTheDocument();
    expect(screen.queryByText('Wrong product')).not.toBeInTheDocument();
    expect(screen.getByLabelText('About')).toHaveValue('instagram.caption');

    await userEvent.type(screen.getByLabelText('Comment'), 'Try mentioning the pier');
    await userEvent.click(screen.getByRole('button', { name: 'Post comment' }));
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({
      body: 'Try mentioning the pier', field: 'instagram.caption',
    });

    await userEvent.click(screen.getByRole('button', { name: 'Show 1 resolved' }));
    expect(screen.getByText('Wrong product')).toBeInTheDocument();
    expect(screen.getByText('Rejected')).toBeInTheDocument();
  });

  it('has no edit button for people who may only read', () => {
    fakeServer({ 'GET /jobs/j1/comments': { body: [] } });
    renderPanel(job(), false);
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
  });
});
