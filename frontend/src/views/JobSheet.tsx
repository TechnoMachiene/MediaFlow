import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, mediaUrl } from '../api';
import {
  CANCELLABLE, addDays, fmt, isAdmin, isReviewer, jobState, localTz, timezones, todayIn,
  tzLabel, wallClock, zonedToInstant,
} from '../lib';
import { Sheet } from '../Sheet';
import { useSession } from '../session';
import { useToast } from '../toast';
import type { Job, Post } from '../types';
import { DraftPanel } from './Draft';
import {
  Button, ConfirmButton, ErrorNote, Field, Icon, JobBadge, Spinner, useLast,
} from '../ui';

/* ── Generated content ───────────────────────────────────────────────────── */

export { ContentView } from './Content';

/* ── Media preview ───────────────────────────────────────────────────────── */

function MediaPreview({ job }: { job: Pick<Job, 'job_id' | 'media_kind' | 'original_filename'> }) {
  const [url, setUrl] = useState<string | null>(null);
  const [state, setState] = useState<'idle' | 'loading' | 'error'>('idle');
  const isImage = job.media_kind === 'image';

  const load = () => {
    setState('loading');
    mediaUrl(job.job_id).then(
      (u) => { setUrl(u); setState('idle'); },
      () => setState('error'),
    );
  };
  useEffect(() => {
    setUrl(null);
    setState('idle');
    if (isImage) load(); // videos can be large, so they load when asked
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.job_id]);

  return (
    <div className="preview">
      {url && isImage && <img src={url} alt={`Uploaded image ${job.original_filename}`} />}
      {url && !isImage && <video src={url} controls playsInline autoPlay />}
      {!url && state === 'loading' && <Spinner />}
      {!url && state === 'error' && (
        <button type="button" className="link" onClick={load}>Couldn't load media. Try again</button>
      )}
      {!url && state === 'idle' && !isImage && (
        <button type="button" className="preview-play" onClick={load}>
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
  const state = jobState(job);
  const canEdit = (owned || isReviewer(user.role)) && job.status === 'done' && !!job.result;

  const canCancel = (owned || isAdmin(user.role)) && CANCELLABLE.has(job.status);
  const canReview =
    isReviewer(user.role) && job.status === 'done' && job.approval_status === 'pending';
  const canSchedule = owned && job.status === 'done' && job.approval_status === 'approved';

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
    run(status, () => api<Job>('PATCH', `/jobs/${job.job_id}/approval`, { status, comment }), (next) => {
      onChanged(next);
      onReviewed?.(next);
      toast(status === 'approved' ? 'Approved' : 'Rejected');
      onClose();
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
      <MediaPreview job={job} />

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
          {(canReview || canSchedule || canCancel) && (
            <div className="job-actions">
              {canReview && (
                <>
                  <Button variant="primary" busy={busy === 'approved'} disabled={!!busy}
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
              Approved drafts can be scheduled by the person who uploaded them. If anyone edits a
              draft later, it comes back here for review.
            </p>
          )}
        </>
      )}

      {job.result && <DraftPanel job={job} canEdit={canEdit} onChanged={onChanged} />}
      {job.result && (
        <p className="footnote">
          Drafted by {job.result.model}
          {job.cached ? ' · reused from identical media' : ''}
        </p>
      )}
    </div>
  );
}
