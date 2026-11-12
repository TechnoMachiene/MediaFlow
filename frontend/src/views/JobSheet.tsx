import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, mediaUrl } from '../api';
import {
  CANCELLABLE, addDays, fmt, isAdmin, isReviewer, jobState, localTz, parseContent,
  timezones, todayIn, tzLabel, wallClock, zonedToInstant,
} from '../lib';
import { Sheet } from '../Sheet';
import { useSession } from '../session';
import { useToast } from '../toast';
import type { Job, Post } from '../types';
import {
  Button, ConfirmButton, CopyButton, ErrorNote, Field, Icon, JobBadge, Segmented, Spinner, useLast,
} from '../ui';

/* ── Generated content ───────────────────────────────────────────────────── */

const SECTION_LABELS: Record<string, string> = {
  instagram: 'Instagram', tiktok: 'TikTok', facebook: 'Facebook', content_summary: 'Summary',
};
const FIELD_LABELS: Record<string, string> = {
  recommended_format: 'Recommended format', caption: 'Caption', post_copy: 'Post',
  hashtags: 'Hashtags', cover_text: 'Cover text', reel_hook: 'Reel hook', hook: 'Hook',
  video_concept: 'Video concept', on_screen_text: 'On-screen text',
  voiceover_script: 'Voiceover script', call_to_action: 'Call to action',
  engagement_question: 'Engagement question', accessibility_description: 'Alt text',
  translated_caption: 'English translation', detected_languages: 'Languages detected',
  primary_content_language: 'Written in', media_type: 'Media type',
  main_message: 'Main message', target_audience: 'Audience', primary_goal: 'Goal',
  suggested_cta: 'Suggested call to action', content_tone: 'Tone', key_topics: 'Key topics',
};
const humanize = (key: string) =>
  key.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

function ContentField({ name, value }: { name: string; value: unknown }) {
  const label = FIELD_LABELS[name] ?? humanize(name);
  if (Array.isArray(value)) {
    const items = value.map(String).filter(Boolean);
    if (!items.length) return null;
    const ordered = name === 'on_screen_text';
    return (
      <section className="content-field">
        <header>
          <h4>{label}</h4>
          <CopyButton text={items.join(ordered ? '\n' : ' ')} />
        </header>
        {ordered ? (
          <ol dir="auto">{items.map((item, i) => <li key={i}>{item}</li>)}</ol>
        ) : (
          <div className="chips" dir="auto">
            {items.map((item, i) => <span key={i} className="chip">{item}</span>)}
          </div>
        )}
      </section>
    );
  }
  const text = typeof value === 'string' ? value.trim() : value == null ? '' : String(value);
  if (!text) return null;
  return (
    <section className="content-field">
      <header>
        <h4>{label}</h4>
        <CopyButton text={text} />
      </header>
      <p dir="auto">{text}</p>
    </section>
  );
}

export function ContentView({ text }: { text: string }) {
  const content = useMemo(() => parseContent(text), [text]);
  const sections = 'structured' in content ? Object.keys(content.structured) : [];
  const [tab, setTab] = useState(sections[0] ?? '');
  const current = sections.includes(tab) ? tab : sections[0];

  if ('raw' in content) {
    return (
      <section className="content-field">
        <header>
          <h4>Generated draft</h4>
          <CopyButton text={content.raw} />
        </header>
        <p dir="auto">{content.raw}</p>
      </section>
    );
  }
  const fields = Object.entries(content.structured[current]);
  return (
    <div className="content">
      {sections.length > 1 && (
        <Segmented
          label="Platform"
          value={current}
          onChange={setTab}
          options={sections.map((s) => ({ value: s, label: SECTION_LABELS[s] ?? humanize(s) }))}
        />
      )}
      {fields.map(([name, value]) => <ContentField key={name} name={name} value={value} />)}
    </div>
  );
}

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
        This adds a planning entry to the calendar. Nothing is published automatically.
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
  const state = jobState(job);

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

  const decide = (status: 'approved' | 'rejected') =>
    run(status, () => api<Job>('PATCH', `/jobs/${job.job_id}/approval`, { status }), (next) => {
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
                  <ConfirmButton label="Reject" confirm="Confirm reject"
                    busy={busy === 'rejected'} onConfirm={() => decide('rejected')} />
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
          {canReview && (
            <p className="footnote">
              A decision is final. Approved drafts can be scheduled by the person who uploaded them.
            </p>
          )}
        </>
      )}

      {job.result && <ContentView text={job.result.text} />}
      {job.result && (
        <p className="footnote">
          Drafted by {job.result.model}
          {job.cached ? ' · reused from identical media' : ''}
        </p>
      )}
    </div>
  );
}
