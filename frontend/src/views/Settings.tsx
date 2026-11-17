import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { ApiError, api } from '../api';
import { useSession } from '../session';
import { useToast } from '../toast';
import type { RetentionPolicy, RetentionPreview, RetentionView, WorkspaceRef } from '../types';
import { Button, ErrorNote, Field, PageHeader, Spinner } from '../ui';

const message = (err: unknown, fallback: string) => (err instanceof ApiError ? err.message : fallback);

type OriginalsMode = 'default' | 'days' | 'forever';

export function Settings() {
  const { user } = useSession();
  return (
    <div className="page">
      <PageHeader title="Settings" subtitle={user.workspace.name} />
      <div className="page-body narrow">
        <WorkspaceName />
        <Retention />
      </div>
    </div>
  );
}

function WorkspaceName() {
  const { user, switchWorkspace } = useSession();
  const toast = useToast();
  const [name, setName] = useState(user.workspace.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const changed = name.trim() && name.trim() !== user.workspace.name;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!changed) return;
    setBusy(true);
    setError('');
    try {
      await api<WorkspaceRef>('PATCH', `/workspaces/${user.workspace.id}`, { name: name.trim() });
      toast('Workspace renamed');
      switchWorkspace(user.workspace.id); // reload who you are, with the new name
    } catch (err) {
      setError(message(err, 'Could not rename the workspace.'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form panel" onSubmit={save}>
      <h3>Workspace</h3>
      <Field label="Name">
        <input value={name} maxLength={120} onChange={(e) => setName(e.target.value)} />
      </Field>
      {error && <ErrorNote message={error} />}
      <div className="form-actions">
        <span className="spacer" />
        <Button variant="primary" type="submit" busy={busy} disabled={!changed}>Rename</Button>
      </div>
    </form>
  );
}

function toPolicy(mode: OriginalsMode, days: number, afterPublish: boolean,
  deleteRejected: boolean, rejectedDays: number): RetentionPolicy {
  return {
    originals_days: mode === 'default' ? null : mode === 'forever' ? 0 : days,
    delete_after_publish: afterPublish,
    rejected_days: deleteRejected ? rejectedDays : null,
  };
}

export function Retention() {
  const { user } = useSession();
  const toast = useToast();
  const url = `/workspaces/${user.workspace.id}/retention`;
  const [view, setView] = useState<RetentionView | null>(null);
  const [mode, setMode] = useState<OriginalsMode>('default');
  // Raw text while typing (so a field can be cleared and retyped); parsed for the policy.
  const [days, setDays] = useState('30');
  const [afterPublish, setAfterPublish] = useState(false);
  const [deleteRejected, setDeleteRejected] = useState(false);
  const [rejectedDays, setRejectedDays] = useState('30');
  const [preview, setPreview] = useState<RetentionPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const adopt = useCallback((v: RetentionView) => {
    setView(v);
    setMode(v.originals_days === null ? 'default' : v.originals_days === 0 ? 'forever' : 'days');
    setDays(String(v.originals_days || v.effective_originals_days || v.default_days || 30));
    setAfterPublish(v.delete_after_publish);
    setDeleteRejected(v.rejected_days !== null);
    setRejectedDays(String(v.rejected_days ?? 30));
    setPreview(v.preview);
  }, []);

  useEffect(() => {
    api<RetentionView>('GET', url).then(adopt, (err) => setError(message(err, 'Could not load the policy.')));
  }, [url, adopt]);

  const asDays = (text: string) => Math.max(1, Math.floor(Number(text)) || 1);
  const policy = toPolicy(mode, asDays(days), afterPublish, deleteRejected, asDays(rejectedDays));
  const saved = view && toPolicy(
    view.originals_days === null ? 'default' : view.originals_days === 0 ? 'forever' : 'days',
    view.originals_days || 0, view.delete_after_publish, view.rejected_days !== null,
    view.rejected_days ?? 0,
  );
  const dirty = !!view && JSON.stringify(policy) !== JSON.stringify(saved);
  const key = JSON.stringify(policy);

  // What would this policy delete right now? Ask the server (a dry run) as the form changes.
  useEffect(() => {
    if (!view || !dirty) {
      if (view) setPreview(view.preview);
      return;
    }
    const t = window.setTimeout(() => {
      api<RetentionPreview>('POST', `${url}/preview`, JSON.parse(key)).then(setPreview, () => setPreview(null));
    }, 300);
    return () => window.clearTimeout(t);
  }, [key, dirty, view, url]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      adopt(await api<RetentionView>('PUT', url, policy));
      toast('Deletion policy saved');
    } catch (err) {
      setError(message(err, 'Could not save the policy.'));
    } finally {
      setBusy(false);
    }
  }

  if (!view) return error ? <ErrorNote message={error} /> : <div className="center"><Spinner /></div>;
  const ceiling = view.max_days;
  const total = preview ? preview.originals_expired + preview.originals_published + preview.rejected_deleted : 0;

  return (
    <form className="form panel" onSubmit={save} aria-labelledby="retention-title">
      <h3 id="retention-title">Deleting uploaded media</h3>
      <p className="quiet">
        Drafts, approvals and calendar entries are always kept. These rules decide when the
        uploaded photos and videos themselves are deleted.
      </p>

      <fieldset className="choices">
        <legend>Keep originals</legend>
        <label>
          <input type="radio" name="originals" checked={mode === 'default'} onChange={() => setMode('default')} />
          Installation default ({view.default_days ? `${view.default_days} days` : 'forever'})
        </label>
        <label>
          <input type="radio" name="originals" checked={mode === 'days'} onChange={() => setMode('days')} />
          For{' '}
          <input
            type="number" min={1} max={ceiling || 36500} value={days} aria-label="Days to keep originals"
            onChange={(e) => { setMode('days'); setDays(e.target.value); }}
          />{' '}
          days after upload or the calendar date
        </label>
        <label className={ceiling ? 'disabled' : ''}>
          <input type="radio" name="originals" checked={mode === 'forever'} disabled={!!ceiling}
            onChange={() => setMode('forever')} />
          Forever{ceiling ? ` (this installation keeps them at most ${ceiling} days)` : ''}
        </label>
      </fieldset>

      <label className="check">
        <input type="checkbox" checked={afterPublish} onChange={(e) => setAfterPublish(e.target.checked)} />
        Delete an original as soon as its post is published everywhere it was sent
      </label>
      <label className="check">
        <input type="checkbox" checked={deleteRejected} onChange={(e) => setDeleteRejected(e.target.checked)} />
        Delete rejected drafts entirely
        {deleteRejected && (
          <>
            {' '}
            <input type="number" min={1} max={36500} value={rejectedDays} aria-label="Days after rejection"
              onChange={(e) => setRejectedDays(e.target.value)} />
            {' '}days after the decision
          </>
        )}
      </label>

      <p className={`callout ${total ? 'callout-red' : ''}`} role="status">
        {preview === null ? 'Checking what this would delete…'
          : total === 0 ? 'Nothing would be deleted right now.'
          : `${dirty ? 'If saved, the next run would delete' : 'The next run will delete'} `
            + [
              preview.originals_expired && `${preview.originals_expired} original(s) past their time`,
              preview.originals_published && `${preview.originals_published} original(s) already published`,
              preview.rejected_deleted && `${preview.rejected_deleted} rejected draft(s)`,
            ].filter(Boolean).join(', ') + '.'}
      </p>
      {error && <ErrorNote message={error} />}
      <div className="form-actions">
        <span className="spacer" />
        <Button variant="primary" type="submit" busy={busy} disabled={!dirty}>Save policy</Button>
      </div>
    </form>
  );
}
