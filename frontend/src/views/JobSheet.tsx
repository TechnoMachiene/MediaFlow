import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, mediaUrl } from '../api';
import {
  CANCELLABLE, addDays, fieldLabel, fmt, isAdmin, isReviewer, jobState, localTz, timezones, todayIn,
  tzLabel, wallClock, zonedToInstant,
} from '../lib';
import { Sheet } from '../Sheet';
import { useSession } from '../session';
import { useToast } from '../toast';
import type { DraftCheck, Job, Post } from '../types';
import { DraftPanel } from './Draft';
import {
  Button, ConfirmButton, ErrorNote, Field, Icon, JobBadge, Spinner, useLast,
} from '../ui';

/* ── Generated content ───────────────────────────────────────────────────── */

export { ContentView } from './Content';

/* ── Media preview ───────────────────────────────────────────────────────── */

const GONE: Record<string, string> = {
  policy: "The original was deleted by this workspace's retention policy. The drafts are kept.",
  published: 'The original was deleted after publishing, as this workspace asks. The drafts are kept.',
  manual: 'The original was deleted on request. The drafts are kept.',
};

function MediaPreview({ job }: {
  job: Pick<Job, 'job_id' | 'media_kind' | 'original_filename' | 'media_deleted_reason'>;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [state, setState] = useState<'idle' | 'loading' | 'error' | 'gone'>('idle');
  const [reason, setReason] = useState('');
  const isImage = job.media_kind === 'image';
  const resumeAt = useRef(0);

  const load = (fresh = false) => {
    setState('loading');
    mediaUrl(job.job_id, { fresh }).then(
      (u) => { setUrl(u); setState('idle'); },
      (err) => {
        const gone = err instanceof ApiError && err.status === 410;
        setReason(gone ? err.message : '');
        setState(gone ? 'gone' : 'error');
      },
    );
  };
  // A link that expired mid-video: get a new one and carry on from the same moment.
  const onVideoError = (e: React.SyntheticEvent<HTMLVideoElement>) => {
    resumeAt.current = e.currentTarget.currentTime;
    setUrl(null);
    load(true);
  };
  useEffect(() => {
    setUrl(null);
    setState('idle');
    if (job.media_deleted_reason) {
      setReason(GONE[job.media_deleted_reason] ?? GONE.manual);
      setState('gone');
      return;
    }
    if (isImage) load(); // videos can be large, so they load when asked
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.job_id]);

  return (
    <div className="preview">
      {url && isImage && <img src={url} alt={`Uploaded image ${job.original_filename}`} />}
      {url && !isImage && (
        <video
          src={url} controls playsInline autoPlay onError={onVideoError}
          onLoadedMetadata={(e) => { if (resumeAt.current) e.currentTarget.currentTime = resumeAt.current; }}
        />
      )}
      {!url && state === 'loading' && <Spinner />}
      {!url && state === 'error' && (
        <button type="button" className="link" onClick={() => load()}>Couldn't load media. Try again</button>
      )}
      {state === 'gone' && <p className="preview-gone">{reason}</p>}
      {!url && state === 'idle' && !isImage && (
        <button type="button" className="preview-play" onClick={() => load()}>
          <Icon name="film" />
          <span>Load video</span>
        </button>
      )}
    </div>
  );
}

/* ── Schedule form ───────────────────────────────────────────────────────── */

export function ScheduleForm({
  initial, submitLabel, busy, error, onSubmit, onCancel,
}: {
  initial?: { at: string; timezone: string };
  submitLabel: string;
  busy: boolean;
  error: string;
  onSubmit: (value: { scheduled_at: string; timezone: string }) => void;
  onCancel: () => void;
}) {
  const [tz, setTz] = useState(initial?.timezone ?? localTz);
  const start = useMemo(() => {
    const at = initial ? new Date(initial.at) : null;
    if (at && at.getTime() > Date.now()) return wallClock(at, initial!.timezone);
    return { date: addDays(todayIn(tz), 1), time: '09:00' };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [date, setDate] = useState(start.date);
  const [time, setTime] = useState(start.time);

  const instant = date && time ? zonedToInstant(date, time, tz) : null;
  const valid = instant !== null && !Number.isNaN(instant.getTime());
  const past = valid && instant.getTime() <= Date.now();
  const options = timezones.includes(tz) ? timezones : [tz, ...timezones];

  // The form opens below the media; bring its actions into view without making anyone hunt.
  const form = useRef<HTMLFormElement>(null);
  useEffect(() => {
    form.current?.closest('.panel')?.scrollIntoView({ block: 'nearest' });
  }, []);

  return (
    <form
      ref={form}
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid && !past) onSubmit({ scheduled_at: instant.toISOString(), timezone: tz });
      }}
    >
      <div className="form-row">
        <Field label="Date" error={past ? 'Choose a time in the future.' : undefined}>
          <input
            type="date" required value={date} min={todayIn(tz)}
            onChange={(e) => setDate(e.target.value)}
          />
        </Field>
        <Field label="Time">
          <input type="time" required value={time} onChange={(e) => setTime(e.target.value)} />
        </Field>
      </div>
      <Field label="Timezone">
        <select value={tz} onChange={(e) => setTz(e.target.value)}>
          {options.map((z) => <option key={z} value={z}>{tzLabel(z)}</option>)}
        </select>
      </Field>
      <p className="footnote">
        This adds an entry to the calendar. To publish it automatically, open the entry and
        choose an account.
      </p>
      {error && <ErrorNote message={error} />}
      <div className="form-actions">
        <Button onClick={onCancel}>Cancel</Button>
        <Button variant="primary" type="submit" busy={busy} disabled={!valid || past}>
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}

/* ── Job sheet ───────────────────────────────────────────────────────────── */

interface Props {
  job: Job | null;
  /** Whether the signed-in person submitted this job. */
  owned: boolean;
  onClose: () => void;
  onChanged: (job: Job) => void;
  onReviewed?: (job: Job) => void;
}

export function JobSheet({ job: current, owned, onClose, onChanged, onReviewed }: Props) {
  const job = useLast(current);
  return (
    <Sheet open={current !== null} onClose={onClose} title={job?.original_filename ?? ''}>
      {job && (
        <JobDetail
          key={job.job_id}
          job={job} owned={owned} onClose={onClose} onChanged={onChanged} onReviewed={onReviewed}
        />
      )}
    </Sheet>
  );
}

function JobDetail({ job, owned, onClose, onChanged, onReviewed }: Props & { job: Job }) {
  const { user } = useSession();
  const toast = useToast();
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [scheduling, setScheduling] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');
  const [factsChecked, setFactsChecked] = useState(false);
  const state = jobState(job);
  const checks = job.checks ?? [];
  const gaps = checks.filter((c) => c.level === 'block');
  const canEdit = (owned || isReviewer(user.role)) && job.status === 'done' && !!job.result;

  const canCancel = (owned || isAdmin(user.role)) && CANCELLABLE.has(job.status);
  const canDeleteOriginal = (owned || isAdmin(user.role)) && !job.media_deleted_reason &&
    !state.active && job.status !== 'cancelled';
  const canReview =
    isReviewer(user.role) && job.status === 'done' && job.approval_status === 'pending';
  const canSchedule = owned && job.status === 'done' && job.approval_status === 'approved';
  const canSend = (owned || isAdmin(user.role)) && job.status === 'done' && !!job.result &&
    job.approval_status === 'with_uploader';

  async function run<T>(name: string, work: () => Promise<T>, then: (value: T) => void) {
    setBusy(name);
    setError('');
    try {
      then(await work());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong.');
    } finally {
      setBusy('');
    }
  }

  const decide = (status: 'approved' | 'rejected', comment?: string) =>
    run(status, () => api<Job>('PATCH', `/jobs/${job.job_id}/approval`, {
      status, comment, checked: status === 'approved' ? factsChecked : undefined,
    }), (next) => {
      onChanged(next);
      onReviewed?.(next);
      toast(status === 'approved' ? 'Approved' : 'Rejected');
      onClose();
    });

  const send = () =>
    run('send', () => api<Job>('POST', `/jobs/${job.job_id}/submit`), (next) => {
      onChanged(next);
      toast('Sent for review');
    });

  const cancel = () =>
    run('cancel', () => api<Job>('DELETE', `/jobs/${job.job_id}`), (next) => {
      onChanged(next);
      toast('Job cancelled');
    });

  const schedule = (value: { scheduled_at: string; timezone: string }) =>
    run('schedule', () => api<Post>('POST', `/jobs/${job.job_id}/schedule`, value), (post) => {
      toast(`Scheduled for ${fmt.dateTime(post.scheduled_at, post.timezone)}`);
      onClose();
    });

  return (
    <div className="job-detail">
      <MediaPreview key={job.media_deleted_reason ?? 'live'} job={job} />

      <div className="job-meta">
        <JobBadge job={job} />
        <span>{job.media_kind === 'video' ? 'Video' : 'Image'}</span>
        <span>Uploaded {fmt.ago(job.created_at)}</span>
        {job.submitter_email && !owned && <span>by {job.submitter_email}</span>}
      </div>

      {job.status !== 'done' && (
        <div className={`callout ${state.tone === 'red' ? 'callout-red' : ''}`}>
          {state.active && <Spinner />}
          <div>
            <strong>{state.note}</strong>
            {job.error && <p>{job.error}</p>}
            {job.attempts > 1 && state.active && <p>Attempt {job.attempts}.</p>}
          </div>
        </div>
      )}

      {scheduling ? (
        <div className="panel">
          <h3>Add to calendar</h3>
          <ScheduleForm
            submitLabel="Schedule"
            busy={busy === 'schedule'}
            error={error}
            onSubmit={schedule}
            onCancel={() => { setScheduling(false); setError(''); }}
          />
        </div>
      ) : (
        <>
          {error && <ErrorNote message={error} />}
          {(canSend || canReview) && <DraftChecks checks={checks} />}
          {canReview && !rejecting && (
            <label className="confirm-facts">
              <input type="checkbox" checked={factsChecked} disabled={gaps.length > 0}
                onChange={(e) => setFactsChecked(e.target.checked)} />
              <span>
                I've checked every name, price, date and place against the brief and fact sheet.
                Gemini's drafts are suggestions and can be wrong.
              </span>
            </label>
          )}
          {canSend && (
            <p className="footnote">
              {owned ? 'Only you can see this draft for now.' : 'The uploader hasn\'t sent this yet.'}
              {' '}Edit anything that's wrong, then send it to a reviewer.
            </p>
          )}
          {(canSend || canReview || canSchedule || canCancel) && (
            <div className="job-actions">
              {canSend && (
                <Button variant="primary" busy={busy === 'send'} disabled={!!busy} onClick={send}>
                  Send for review
                </Button>
              )}
              {canReview && (
                <>
                  <Button variant="primary" busy={busy === 'approved'}
                    disabled={!!busy || !factsChecked || gaps.length > 0}
                    onClick={() => decide('approved')}>
                    Approve
                  </Button>
                  {!rejecting && (
                    <Button variant="danger" disabled={!!busy} onClick={() => setRejecting(true)}>
                      Reject…
                    </Button>
                  )}
                </>
              )}
              {canSchedule && (
                <Button variant="primary" onClick={() => setScheduling(true)}>
                  <Icon name="calendar" /> Schedule…
                </Button>
              )}
              {canCancel && (
                <ConfirmButton label="Cancel job" confirm="Confirm cancel"
                  busy={busy === 'cancel'} onConfirm={cancel} />
              )}
            </div>
          )}
          {canDeleteOriginal && (
            <div className="job-actions">
              <ConfirmButton
                label="Delete original" confirm="Delete the file? Drafts stay"
                busy={busy === 'delete-original'}
                onConfirm={() => run('delete-original', async () => {
                  await api<void>('DELETE', `/jobs/${job.job_id}/media`);
                  return api<Job>('GET', `/jobs/${job.job_id}`);
                }, (next) => { onChanged(next); toast('Original deleted. The drafts are kept.'); })}
              />
            </div>
          )}
          {canReview && rejecting && (
            <form
              className="form panel"
              onSubmit={(e) => { e.preventDefault(); if (reason.trim()) void decide('rejected', reason.trim()); }}
            >
              <Field label="What needs to change?" hint="Shared with the uploader in the comments.">
                <textarea rows={3} maxLength={2000} autoFocus value={reason}
                  onChange={(e) => setReason(e.target.value)} />
              </Field>
              <div className="form-actions">
                <span className="spacer" />
                <Button onClick={() => { setRejecting(false); setReason(''); }}>Cancel</Button>
                <Button variant="danger" type="submit" busy={busy === 'rejected'} disabled={!reason.trim()}>
                  Reject
                </Button>
              </div>
            </form>
          )}
          {canReview && !rejecting && (
            <p className="footnote">
              Approved drafts can be scheduled by the person who uploaded them. If the uploader
              edits a draft later, it goes back to them until they send it again; if a reviewer
              edits it, it comes back here.
            </p>
          )}
        </>
      )}

      {job.result && <DraftPanel job={job} owned={owned} canEdit={canEdit} onChanged={onChanged} />}
      {job.result && (
        <p className="footnote">
          Drafted by {job.result.model}
          {job.cached ? ' · reused from identical media' : ''}
        </p>
      )}
    </div>
  );
}

/** What to verify before approving (server-side rules: app/draft_checks.py). */
function DraftChecks({ checks }: { checks: DraftCheck[] }) {
  if (!checks.length) return null;
  const gaps = checks.filter((c) => c.level === 'block');
  return (
    <div className={`callout checks ${gaps.length ? 'callout-red' : ''}`}>
      <strong>{gaps.length ? 'Fix before approving' : 'Check before approving'}</strong>
      <ul>
        {checks.map((c, i) => (
          <li key={i} className={c.level}>
            {c.field && <span className="quiet">{fieldLabel(c.field)}: </span>}{c.message}
          </li>
        ))}
      </ul>
    </div>
  );
}
