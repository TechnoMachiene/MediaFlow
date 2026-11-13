import { useEffect, useState } from 'react';
import { api } from '../api';
import { useJobs } from '../hooks';
import { fmt, jobState } from '../lib';
import { useSession } from '../session';
import type { Stats } from '../types';
import { Badge, Button, ErrorNote, Icon, JobBadge, PageHeader, Segmented, Spinner } from '../ui';
import { JobSheet } from './JobSheet';

type Kind = 'all' | 'image' | 'video';
const KINDS: { value: Kind; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'image', label: 'Images' },
  { value: 'video', label: 'Videos' },
];

const TILES: { key: keyof Stats; label: string; hint: string }[] = [
  { key: 'ready', label: 'Waiting', hint: 'Queued for a worker' },
  { key: 'processing', label: 'Analyzing', hint: 'With a worker now' },
  { key: 'deferred', label: 'Retrying', hint: 'Backing off after an error' },
  { key: 'dead', label: 'Failed', hint: 'Out of retries' },
  { key: 'gemini_calls_today', label: 'Gemini calls today', hint: 'All workspaces · daily quota' },
];

type Health = 'checking' | 'ready' | 'down';

export function Activity() {
  const { user } = useSession();
  const [stats, setStats] = useState<Stats | null>(null);
  const [health, setHealth] = useState<Health>('checking');
  const [kind, setKind] = useState<Kind>('all');
  const { jobs, more, error, loadingMore, reload, loadMore, replace } = useJobs(
    kind === 'all' ? '/jobs' : `/jobs?kind=${kind}`,
    (list) => list.some((j) => jobState(j).active),
  );
  const [openId, setOpenId] = useState<string | null>(null);
  const open = jobs?.find((j) => j.job_id === openId) ?? null;

  useEffect(() => {
    let alive = true;
    const tick = () => {
      if (document.hidden) return;
      api<Stats>('GET', '/stats').then((s) => alive && setStats(s), () => {});
      fetch('/health/ready').then(
        (r) => alive && setHealth(r.ok ? 'ready' : 'down'),
        () => alive && setHealth('down'),
      );
    };
    tick();
    const timer = window.setInterval(tick, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <div className="page">
      <PageHeader title="Activity" subtitle={`Queue health and every job in ${user.workspace.name}`}>
        {health !== 'checking' && (
          <Badge tone={health === 'ready' ? 'green' : 'red'}>
            {health === 'ready' ? 'All systems ready' : 'Database or Redis unavailable'}
          </Badge>
        )}
      </PageHeader>
      <div className="page-body">
        <dl className="stats">
          {TILES.filter((t) => t.key !== 'gemini_calls_today' || stats?.gemini_calls_today != null).map((t) => (
            <div key={t.key} className="stat">
              <dt>{t.label}</dt>
              <dd className={t.key === 'dead' && stats?.dead ? 'stat-alert' : ''}>
                {stats ? (stats[t.key] ?? 0).toLocaleString() : '–'}
              </dd>
              <span>{t.hint}</span>
            </div>
          ))}
        </dl>

        <div className="toolbar">
          <h2 className="section-title">All jobs</h2>
          <Segmented label="Media type" value={kind} onChange={setKind} options={KINDS} />
        </div>

        {error && <ErrorNote message={error} onRetry={reload} />}
        {jobs === null && !error && <div className="center"><Spinner /></div>}
        {jobs?.length === 0 && <p className="quiet">No jobs yet.</p>}
        {jobs && jobs.length > 0 && (
          <ul className="rows">
            {jobs.map((job) => (
              <li key={job.job_id}>
                <button type="button" className="row" onClick={() => setOpenId(job.job_id)}>
                  <span className="row-icon"><Icon name={job.media_kind === 'video' ? 'film' : 'photo'} /></span>
                  <span className="row-main">
                    <span className="row-title">{job.original_filename}</span>
                    <span className="row-sub">
                      {job.submitter_email} · {fmt.ago(job.created_at)}
                      {job.attempts > 1 ? ` · ${job.attempts} attempts` : ''}
                    </span>
                  </span>
                  <span className="row-side"><JobBadge job={job} /></span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {more && (
          <div className="more"><Button busy={loadingMore} onClick={loadMore}>Show more</Button></div>
        )}
      </div>
      <JobSheet
        job={open}
        owned={open?.submitter_email === user.email}
        onClose={() => setOpenId(null)}
        onChanged={replace}
      />
    </div>
  );
}
