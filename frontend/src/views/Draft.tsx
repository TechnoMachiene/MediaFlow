import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { useLive } from '../live';
import { ApiError, api } from '../api';
import {
  FIELD_LABELS, SECTION_LABELS, diffContent, fieldLabel, fieldPaths, fmt, humanize, parseContent,
} from '../lib';
import { useToast } from '../toast';
import type { DraftComment, DraftSaved, DraftVersion, Job } from '../types';
import { Badge, Button, ErrorNote, Field, Segmented, Spinner } from '../ui';
import { ContentView } from './Content';

type Tab = 'draft' | 'history' | 'comments';
const message = (err: unknown, fallback: string) => (err instanceof ApiError ? err.message : fallback);

/**
 * The draft, its version history and its review thread. Shown to the uploader and reviewers;
 * `canEdit` is the same for both (the server enforces it too).
 */
export function DraftPanel({
  job, owned = false, canEdit, onChanged,
}: { job: Job; owned?: boolean; canEdit: boolean; onChanged: (job: Job) => void }) {
  const [tab, setTab] = useState<Tab>('draft');
  const [editing, setEditing] = useState(false);
  const [comments, setComments] = useState<DraftComment[]>([]);
  const [anchor, setAnchor] = useState<string | null>(null);
  const text = job.result?.text ?? '';
  const version = job.draft_version ?? 1;

  const loadComments = useCallback(() => {
    api<DraftComment[]>('GET', `/jobs/${job.job_id}/comments`).then(setComments, () => {});
  }, [job.job_id]);
  useEffect(loadComments, [loadComments]);
  useLive(['jobs'], loadComments);

  const refresh = useCallback(async () => {
    onChanged(await api<Job>('GET', `/jobs/${job.job_id}`));
  }, [job.job_id, onChanged]);

  const open = comments.filter((c) => !c.parent_id && !c.resolved_at);
  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const c of open) if (c.field) out[c.field] = (out[c.field] ?? 0) + 1;
    return out;
  }, [open]);

  if (!job.result) return null;

  return (
    <div className="draft">
      <div className="draft-bar">
        <Segmented
          label="Draft view" value={tab} onChange={(t) => { setTab(t); setEditing(false); }}
          options={[
            { value: 'draft', label: version > 1 ? `Draft · v${version}` : 'Draft' },
            { value: 'history', label: 'History' },
            { value: 'comments', label: open.length ? `Comments (${open.length})` : 'Comments' },
          ]}
        />
        {tab === 'draft' && canEdit && !editing && (
          <Button onClick={() => setEditing(true)}>Edit</Button>
        )}
      </div>

      {tab === 'draft' && (editing ? (
        <DraftEditor
          job={job}
          owned={owned}
          onCancel={() => setEditing(false)}
          onSaved={async () => { setEditing(false); await refresh(); }}
          onReload={refresh}
        />
      ) : (
        <ContentView
          text={text}
          notes={{ counts, onComment: (path) => { setAnchor(path); setTab('comments'); } }}
        />
      ))}
      {tab === 'history' && (
        <DraftHistory job={job} canEdit={canEdit} onRestored={refresh} />
      )}
      {tab === 'comments' && (
        <DraftComments
          job={job} comments={comments} onChanged={loadComments}
          anchor={anchor} onAnchor={setAnchor}
        />
      )}
    </div>
  );
}

/* ── Editor ──────────────────────────────────────────────────────────────── */

type Structured = Record<string, Record<string, unknown>>;

function clone(text: string): Structured | string {
  const content = parseContent(text);
  return 'raw' in content ? content.raw : JSON.parse(JSON.stringify(content.structured));
}

const serialize = (draft: Structured | string) =>
  typeof draft === 'string' ? draft : JSON.stringify(draft, null, 2);

/** What saving does to the draft's place in the workflow (mirrors app/api/drafts.py). */
function saveEffect(job: Job, owned: boolean): string | null {
  const status = job.approval_status;
  if (status === 'with_uploader') {
    return owned
      ? "Your changes stay with you. Send the draft for review when it's ready."
      : "The uploader hasn't sent this draft for review yet. Your version stays with them.";
  }
  if (owned) {
    return status === 'pending'
      ? 'Saving a change takes this draft out of review until you send it again.'
      : `This draft is ${status}. Saving a change brings it back to you to send for review again.`;
  }
  return status === 'pending' ? null : `This draft is ${status}. Saving a change sends it back for review.`;
}

export function DraftEditor({
  job, owned = false, onCancel, onSaved, onReload,
}: {
  job: Job; owned?: boolean; onCancel: () => void; onSaved: () => void; onReload: () => void;
}) {
  const toast = useToast();
  // What this edit started from. Live updates may bring a newer version in while typing; the
  // save still names the version it was based on, so the server refuses to overwrite theirs.
  const [base] = useState(() => ({ version: job.draft_version ?? 1, text: job.result!.text }));
  const original = base.text;
  const newer = (job.draft_version ?? 1) !== base.version;
  const [draft, setDraft] = useState(() => clone(original));
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [stale, setStale] = useState(false);
  const dirty = serialize(draft) !== serialize(clone(original));

  function setField(section: string, field: string, value: unknown) {
    setDraft((d) => (typeof d === 'string' ? d
      : { ...d, [section]: { ...d[section], [field]: value } }));
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!dirty) return;
    setBusy(true);
    setError('');
    try {
      const out = await api<DraftSaved>('POST', `/jobs/${job.job_id}/versions`, {
        base_version: base.version, text: serialize(draft), note: note.trim() || undefined,
      });
      const n = out.version.number;
      toast(out.returned_to_review ? `Saved as version ${n}. It's back in review.`
        : out.approval_status === 'with_uploader' && owned
          ? `Saved as version ${n}. Send it for review when it's ready.`
          : `Saved as version ${n}`);
      onSaved();
    } catch (err) {
      setError(message(err, 'Could not save the draft.'));
      setStale(err instanceof ApiError && err.status === 409 && /while you were editing/.test(err.message));
      setBusy(false);
    }
  }

  return (
    <form className="form draft-editor" onSubmit={save} noValidate>
      {saveEffect(job, owned) && <p className="callout">{saveEffect(job, owned)}</p>}
      {newer && (
        <p className="callout callout-red" role="status">
          Someone saved version {job.draft_version} while you were editing. Saving now will be
          refused; copy anything you need, then Cancel and edit the new version.
        </p>
      )}
      {typeof draft === 'string' ? (
        <Field label="Draft">
          <textarea rows={12} value={draft} dir="auto" onChange={(e) => setDraft(e.target.value)} />
        </Field>
      ) : (
        Object.entries(draft).map(([section, fields]) => (
          <fieldset key={section} className="draft-section">
            <legend>{SECTION_LABELS[section] ?? humanize(section)}</legend>
            {Object.entries(fields).map(([field, value]) => {
              const label = FIELD_LABELS[field] ?? humanize(field);
              if (Array.isArray(value)) {
                return (
                  <Field key={field} label={label} hint="One per line">
                    <textarea
                      rows={Math.min(8, Math.max(2, value.length))} dir="auto"
                      value={value.map(String).join('\n')}
                      onChange={(e) => setField(section, field, e.target.value.split('\n'))}
                      onBlur={(e) => setField(section, field,
                        e.target.value.split('\n').map((s) => s.trim()).filter(Boolean))}
                    />
                  </Field>
                );
              }
              if (value !== null && typeof value === 'object') return null; // not hand-editable
              const long = String(value ?? '').length > 60;
              return (
                <Field key={field} label={label}>
                  {long ? (
                    <textarea rows={4} dir="auto" value={String(value ?? '')}
                      onChange={(e) => setField(section, field, e.target.value)} />
                  ) : (
                    <input dir="auto" value={String(value ?? '')}
                      onChange={(e) => setField(section, field, e.target.value)} />
                  )}
                </Field>
              );
            })}
          </fieldset>
        ))
      )}
      <Field label="What changed (optional)">
        <input maxLength={280} value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. Shorter caption, fixed product name" />
      </Field>
      {error && <ErrorNote message={error} onRetry={stale ? onReload : undefined} />}
      <div className="form-actions">
        <span className="spacer" />
        <Button onClick={onCancel}>Cancel</Button>
        <Button variant="primary" type="submit" busy={busy} disabled={!dirty}>Save version</Button>
      </div>
    </form>
  );
}

/* ── History ─────────────────────────────────────────────────────────────── */

function author(v: DraftVersion) {
  return v.author_email ?? 'Gemini';
}

function describeVersion(v: DraftVersion) {
  if (v.source === 'ai') return 'Original draft';
  if (v.source === 'restore') return `Restored version ${v.restored_from}`;
  return v.note ?? 'Edited';
}

export function DraftHistory({
  job, canEdit, onRestored,
}: { job: Job; canEdit: boolean; onRestored: () => void }) {
  const toast = useToast();
  const [versions, setVersions] = useState<DraftVersion[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [full, setFull] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<DraftVersion[]>('GET', `/jobs/${job.job_id}/versions`).then(
      (list) => { setVersions(list); setOpen(list[0]?.number ?? null); },
      (err) => setError(message(err, 'Could not load the history.')),
    );
  }, [job.job_id, job.draft_version]);

  async function restore(number: number) {
    setBusy(true);
    setError('');
    try {
      await api<DraftSaved>('POST', `/jobs/${job.job_id}/versions`, {
        base_version: job.draft_version ?? 1, restore: number,
      });
      toast(`Version ${number} restored as a new version`);
      onRestored();
    } catch (err) {
      setError(message(err, 'Could not restore that version.'));
    } finally {
      setBusy(false);
    }
  }

  if (error) return <ErrorNote message={error} />;
  if (!versions) return <div className="center"><Spinner /></div>;

  return (
    <ol className="rows history" aria-label="Versions">
      {versions.map((v, i) => {
        const previous = versions[i + 1];
        const changes = previous ? diffContent(previous.text, v.text) : [];
        const expanded = open === v.number;
        return (
          <li key={v.number}>
            <button
              type="button" className="row" aria-expanded={expanded}
              onClick={() => { setOpen(expanded ? null : v.number); setFull(false); }}
            >
              <span className="avatar" aria-hidden="true">{author(v)[0].toUpperCase()}</span>
              <span className="row-main">
                <span className="row-title">Version {v.number} · {describeVersion(v)}</span>
                <span className="row-sub">{author(v)} · {fmt.ago(v.created_at)}</span>
              </span>
              <span className="row-side">{v.current && <Badge tone="blue">Current</Badge>}</span>
            </button>
            {expanded && (
              <div className="history-detail">
                {previous && (changes.length ? (
                  <ul className="diff" aria-label={`Changes from version ${previous.number}`}>
                    {changes.map((c) => (
                      <li key={c.path}>
                        <h4>{fieldLabel(c.path)}</h4>
                        {c.before && <del dir="auto">{c.before}</del>}
                        {c.after && <ins dir="auto">{c.after}</ins>}
                      </li>
                    ))}
                  </ul>
                ) : <p className="quiet">Same text as version {previous.number}.</p>)}
                {(full || !previous) ? <ContentView text={v.text} /> : (
                  <button type="button" className="link" onClick={() => setFull(true)}>
                    Show the whole draft
                  </button>
                )}
                {canEdit && !v.current && (
                  <div className="form-actions">
                    <span className="spacer" />
                    <Button busy={busy} onClick={() => void restore(v.number)}>
                      Restore version {v.number}
                    </Button>
                  </div>
                )}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/* ── Comments ────────────────────────────────────────────────────────────── */

const KIND_LABEL = { approved: 'Approved', rejected: 'Rejected' } as const;

function Composer({
  job, parentId, anchor, onAnchor, onPosted, autoFocus,
}: {
  job: Job; parentId?: string; anchor?: string | null; onAnchor?: (path: string | null) => void;
  onPosted: () => void; autoFocus?: boolean;
}) {
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const paths = useMemo(() => fieldPaths(job.result?.text ?? ''), [job.result?.text]);

  async function post(e: FormEvent) {
    e.preventDefault();
    if (!body.trim()) return;
    setBusy(true);
    setError('');
    try {
      await api<DraftComment>('POST', `/jobs/${job.job_id}/comments`, {
        body: body.trim(), parent_id: parentId, field: parentId ? undefined : anchor ?? undefined,
      });
      setBody('');
      onAnchor?.(null);
      onPosted();
    } catch (err) {
      setError(message(err, 'Could not post the comment.'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form composer" onSubmit={post} noValidate>
      {!parentId && paths.length > 0 && (
        <Field label="About">
          <select value={anchor ?? ''} onChange={(e) => onAnchor?.(e.target.value || null)}>
            <option value="">The whole draft</option>
            {paths.map((p) => <option key={p} value={p}>{fieldLabel(p)}</option>)}
          </select>
        </Field>
      )}
      <Field label={parentId ? 'Reply' : 'Comment'}>
        <textarea
          rows={parentId ? 2 : 3} maxLength={2000} value={body} autoFocus={autoFocus} dir="auto"
          onChange={(e) => setBody(e.target.value)}
        />
      </Field>
      {error && <ErrorNote message={error} />}
      <div className="form-actions">
        <span className="spacer" />
        <Button variant="primary" type="submit" busy={busy} disabled={!body.trim()}>
          {parentId ? 'Reply' : 'Post comment'}
        </Button>
      </div>
    </form>
  );
}

export function DraftComments({
  job, comments, onChanged, anchor, onAnchor,
}: {
  job: Job; comments: DraftComment[]; onChanged: () => void;
  anchor: string | null; onAnchor: (path: string | null) => void;
}) {
  const [replying, setReplying] = useState<string | null>(null);
  const [showResolved, setShowResolved] = useState(false);
  const [error, setError] = useState('');
  const roots = comments.filter((c) => !c.parent_id);
  const replies = (id: string) => comments.filter((c) => c.parent_id === id);
  const current = job.draft_version ?? 1;
  const open = roots.filter((c) => !c.resolved_at);
  const resolved = roots.filter((c) => c.resolved_at);

  async function resolve(c: DraftComment, value: boolean) {
    setError('');
    try {
      await api<DraftComment>('PATCH', `/jobs/${job.job_id}/comments/${c.id}`, { resolved: value });
      onChanged();
    } catch (err) {
      setError(message(err, 'Could not update the thread.'));
    }
  }

  const thread = (c: DraftComment) => (
    <li key={c.id} className={`thread ${c.resolved_at ? 'thread-resolved' : ''}`}>
      <Note c={c} current={current} />
      {replies(c.id).map((r) => <Note key={r.id} c={r} current={current} reply />)}
      <div className="thread-actions">
        {!c.resolved_at && (
          <button type="button" className="link" onClick={() => setReplying(replying === c.id ? null : c.id)}>
            Reply
          </button>
        )}
        <button type="button" className="link" onClick={() => void resolve(c, !c.resolved_at)}>
          {c.resolved_at ? 'Reopen' : 'Resolve'}
        </button>
      </div>
      {replying === c.id && (
        <Composer job={job} parentId={c.id} autoFocus
          onPosted={() => { setReplying(null); onChanged(); }} />
      )}
    </li>
  );

  return (
    <div className="comments">
      {error && <ErrorNote message={error} />}
      {open.length === 0 && <p className="quiet">No open comments.</p>}
      <ol className="threads" aria-label="Open comments">{open.map(thread)}</ol>
      {resolved.length > 0 && (
        <>
          <button type="button" className="link" onClick={() => setShowResolved(!showResolved)}>
            {showResolved ? 'Hide' : 'Show'} {resolved.length} resolved
          </button>
          {showResolved && <ol className="threads" aria-label="Resolved comments">{resolved.map(thread)}</ol>}
        </>
      )}
      <Composer job={job} anchor={anchor} onAnchor={onAnchor} onPosted={onChanged} autoFocus={anchor !== null} />
    </div>
  );
}

function Note({ c, current, reply }: { c: DraftComment; current: number; reply?: boolean }) {
  return (
    <article className={`note ${reply ? 'note-reply' : ''}`}>
      <header>
        <strong>{c.author_email}</strong>
        <span className="quiet">{fmt.ago(c.created_at)}</span>
        {c.kind !== 'comment' && (
          <Badge tone={c.kind === 'approved' ? 'green' : 'red'}>{KIND_LABEL[c.kind]}</Badge>
        )}
      </header>
      {!reply && (c.field || c.version !== current) && (
        <p className="note-anchor">
          {c.field ? fieldLabel(c.field) : 'Whole draft'}
          {c.version !== current && ` · on version ${c.version}`}
        </p>
      )}
      <p dir="auto">{c.body}</p>
      {c.resolved_at && !reply && (
        <p className="quiet">Resolved by {c.resolved_by_email} {fmt.ago(c.resolved_at)}</p>
      )}
    </article>
  );
}
