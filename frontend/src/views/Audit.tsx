import { useCallback, useEffect, useState } from 'react';
import { useLive } from '../live';
import { ApiError, api } from '../api';
import { fmt, roleLabel } from '../lib';
import { useSession } from '../session';
import type { AuditEvent, AuditPage, Role } from '../types';
import { Button, Empty, ErrorNote, PageHeader, Segmented, Spinner } from '../ui';

type Category = 'all' | 'member' | 'invitation' | 'job' | 'draft' | 'post' | 'connection'
  | 'publication' | 'media' | 'workspace';
const CATEGORIES: { value: Category; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'member', label: 'People' },
  { value: 'invitation', label: 'Invites' },
  { value: 'job', label: 'Reviews' },
  { value: 'draft', label: 'Edits' },
  { value: 'post', label: 'Calendar' },
  { value: 'publication', label: 'Publishing' },
  { value: 'connection', label: 'Accounts' },
  { value: 'media', label: 'Deletions' },
  { value: 'workspace', label: 'Workspace' },
];

const role = (value: unknown) => roleLabel[value as Role] ?? String(value);
const when = (value: unknown, tz: unknown) =>
  typeof value === 'string' ? fmt.dateTime(value, typeof tz === 'string' ? tz : 'UTC') : '';

/** One plain sentence per event, so the trail reads like a history, not a table dump. */
export function describe(e: AuditEvent): string {
  const who = e.actor_email === 'system:cli' ? 'The admin command' : e.actor_email;
  const what = e.target_label ?? e.target_id;
  const b = e.before ?? {};
  const a = e.after ?? {};
  switch (e.action) {
    case 'member.created': return `${who} created an account for ${what} as ${role(a.role)}`;
    case 'member.role_changed': return `${who} changed ${what} from ${role(b.role)} to ${role(a.role)}`;
    case 'member.email_changed': return `${who} changed ${b.email}'s sign-in email to ${a.email}`;
    case 'member.password_reset': return `${who} reset the password of ${what}`;
    case 'member.removed': {
      const jobs = Number(a.jobs_deleted ?? 0);
      return `${who} removed ${what} (${role(b.role)})${jobs ? ` and ${jobs} of their jobs` : ''}`;
    }
    case 'member.joined': return `${what} joined as ${role(a.role)} by invitation`;
    case 'member.left': return `${what} left the workspace`;
    case 'invitation.created': return `${who} invited ${what} as ${role(a.role)}`;
    case 'invitation.revoked': return `${who} withdrew the invitation for ${what}`;
    case 'job.approved': return `${who} approved ${what}${a.owner ? ` by ${a.owner}` : ''}`;
    case 'job.rejected': return `${who} rejected ${what}${a.owner ? ` by ${a.owner}` : ''}`;
    case 'job.cancelled': return `${who} cancelled ${what}`;
    case 'post.scheduled': return `${who} scheduled ${what} for ${when(a.scheduled_at, a.timezone)}`;
    case 'post.rescheduled':
      return `${who} moved ${what} from ${when(b.scheduled_at, b.timezone)} to ${when(a.scheduled_at, a.timezone)}`;
    case 'post.cancelled': return `${who} took ${what} off the calendar`;
    case 'draft.edited':
      return a.restored_from
        ? `${who} restored version ${a.restored_from} of ${what} as version ${a.version}`
        : `${who} saved version ${a.version} of ${what}${
          a.approval_status === 'pending' && b.approval_status !== 'pending' ? ', sending it back for review'
            : a.approval_status === 'with_uploader' && b.approval_status !== 'with_uploader'
              ? ', taking it back to the uploader' : ''}`;
    case 'draft.submitted': return `${who} sent version ${a.version} of ${what} for review`;
    case 'connection.connected':
      return `${who} ${a.reconnected ? 'reconnected' : 'connected'} ${what} (${a.provider}, ${a.channel})`;
    case 'connection.disconnected': return `${who} disconnected ${what}`;
    case 'publication.requested': return `${who} set a post to publish to ${what}`;
    case 'publication.retried': return `${who} sent a post to ${what} again`;
    case 'publication.cancelled': return `${who} cancelled publishing a post to ${what}`;
    case 'media.deleted':
      return a.reason === 'manual' ? `${who} deleted the original of ${what}`
        : a.reason === 'published' ? `The retention policy deleted the original of ${what} after publishing`
        : `The retention policy deleted the original of ${what} (after ${a.days} days)`;
    case 'job.purged': return `The retention policy deleted the rejected draft ${what} (after ${a.days} days)`;
    case 'workspace.retention_changed': return `${who} changed the deletion policy`;
    case 'workspace.created': return `${who} created the workspace ${a.name}`;
    case 'workspace.renamed': return `${who} renamed the workspace from ${b.name} to ${a.name}`;
    default: return `${who}: ${e.action} ${what}`;
  }
}

export function Audit() {
  const { user } = useSession();
  const [category, setCategory] = useState<Category>('all');
  const [items, setItems] = useState<AuditEvent[] | null>(null);
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async (before: number | null) => {
    const params = new URLSearchParams({ limit: '50' });
    if (category !== 'all') params.set('category', category);
    if (before) params.set('before', String(before));
    try {
      const page = await api<AuditPage>('GET', `/audit-events?${params}`);
      setItems((prev) => (before && prev ? [...prev, ...page.items] : page.items));
      setNext(page.next_before);
      setError('');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load the audit trail.');
    }
  }, [category]);

  useLive(['audit'], () => load(null));
  useEffect(() => {
    setItems(null);
    void load(null);
  }, [load]);

  return (
    <div className="page">
      <PageHeader title="Audit" subtitle={`Who changed what in ${user.workspace.name}`} />
      <div className="page-body narrow">
        <Segmented label="Show" value={category} onChange={setCategory} options={CATEGORIES} />
        {error && <ErrorNote message={error} onRetry={() => load(null)} />}
        {items === null && !error && <div className="center"><Spinner /></div>}
        {items?.length === 0 && (
          <Empty icon="lock" title="Nothing recorded yet">
            Role changes, invitations, review decisions and calendar changes appear here.
          </Empty>
        )}
        {items && items.length > 0 && (
          <ol className="rows" aria-label="Audit trail">
            {items.map((e) => (
              <li key={e.id} className="row">
                <span className="avatar" aria-hidden="true">{e.actor_email[0].toUpperCase()}</span>
                <span className="row-main">
                  <span className="row-title">{describe(e)}</span>
                  <span className="row-sub" title={e.request_id ? `Request ${e.request_id}` : undefined}>
                    <time dateTime={e.created_at}>{fmt.ago(e.created_at)}</time> · {e.action}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        )}
        {next && (
          <div className="more">
            <Button
              busy={loadingMore}
              onClick={async () => { setLoadingMore(true); await load(next); setLoadingMore(false); }}
            >
              Show older
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
