import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLive } from '../live';
import { ApiError, api } from '../api';
import {
  DELIVERY, addMonths, fmt, headline, localTz, monthGrid, timezones, todayIn, tzLabel,
} from '../lib';
import { Sheet } from '../Sheet';
import { useToast } from '../toast';
import type { CalendarData, Delivery, Post } from '../types';
import {
  Badge, Button, ConfirmButton, ErrorNote, Icon, PageHeader, Thumb, useLast,
} from '../ui';
import { ContentView, ScheduleForm } from './JobSheet';
import { PostPublishing } from './Publishing';

const TZ_KEY = 'mediaflow.tz';

/** Posted or planned. Older servers don't send it: fall back on the time. */
function deliveryOf(p: Post): Delivery {
  if (p.delivery) return p.delivery;
  if (p.status === 'cancelled') return 'cancelled';
  return new Date(p.scheduled_at) < new Date() ? 'past' : 'upcoming';
}

/** The day's dot on phones: what still needs attention wins, then what's coming. */
function dayDelivery(posts: Post[]): Delivery {
  const states = posts.map(deliveryOf);
  for (const d of ['failed', 'sending', 'upcoming', 'published', 'past'] as const) {
    if (states.includes(d)) return d;
  }
  return 'cancelled';
}
function savedTz(): string {
  try {
    const tz = localStorage.getItem(TZ_KEY);
    if (tz && timezones.includes(tz)) return tz;
  } catch {
    /* fall through */
  }
  return localTz;
}

export function Calendar() {
  const [tz, setTz] = useState(savedTz);
  const today = todayIn(tz);
  const [month, setMonth] = useState(today.slice(0, 7));
  const [selected, setSelected] = useState(today);
  const [byDay, setByDay] = useState<Record<string, Post[]> | null>(null);
  const [error, setError] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);

  const days = useMemo(() => monthGrid(month), [month]);

  const load = useCallback(async () => {
    try {
      const data = await api<CalendarData>(
        'GET',
        `/calendar?start=${days[0]}&end=${days[41]}&timezone=${encodeURIComponent(tz)}`,
      );
      setByDay(Object.fromEntries(data.days.map((d) => [d.date, d.posts])));
      setError('');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load the calendar.');
    }
  }, [days, tz]);

  useLive(['calendar', 'jobs'], load);
  useEffect(() => {
    void load();
  }, [load]);

  const go = (next: string) => {
    setMonth(next);
    setSelected(next === today.slice(0, 7) ? today : `${next}-01`);
  };
  const pickTz = (next: string) => {
    setTz(next);
    try {
      localStorage.setItem(TZ_KEY, next);
    } catch {
      /* preference just won't persist */
    }
  };

  const agenda = byDay?.[selected] ?? [];
  const open = useMemo(
    () => Object.values(byDay ?? {}).flat().find((p) => p.id === openId) ?? null,
    [byDay, openId],
  );

  return (
    <div className="page">
      <PageHeader title="Calendar" subtitle="Planned posts. Only entries you send to an account publish." />
      <div className="page-body">
        <div className="toolbar cal-toolbar">
          <div className="cal-nav">
            <button type="button" className="icon-btn" aria-label="Previous month"
              onClick={() => go(addMonths(month, -1))}>
              <Icon name="left" />
            </button>
            <h2 aria-live="polite">{fmt.month(month)}</h2>
            <button type="button" className="icon-btn" aria-label="Next month"
              onClick={() => go(addMonths(month, 1))}>
              <Icon name="right" />
            </button>
            <Button onClick={() => go(today.slice(0, 7))} disabled={month === today.slice(0, 7) && selected === today}>
              Today
            </Button>
          </div>
          <label className="tz-select">
            <span className="visually-hidden">Timezone</span>
            <select value={tz} onChange={(e) => pickTz(e.target.value)}>
              {timezones.map((z) => <option key={z} value={z}>{tzLabel(z)}</option>)}
            </select>
          </label>
        </div>

        {error && <ErrorNote message={error} onRetry={load} />}

        <div className={`cal ${byDay === null ? 'cal-loading' : ''}`}>
          <div className="cal-weekdays" aria-hidden="true">
            {days.slice(0, 7).map((d) => <span key={d}>{fmt.weekday(d)}</span>)}
          </div>
          <div className="cal-grid">
            {days.map((day) => {
              const posts = byDay?.[day] ?? [];
              const live = posts.filter((p) => p.status !== 'cancelled');
              const classes = [
                'cal-day',
                day.slice(0, 7) !== month && 'cal-outside',
                day === today && 'cal-today',
                day === selected && 'cal-selected',
              ].filter(Boolean).join(' ');
              return (
                <button
                  key={day} type="button" className={classes}
                  aria-pressed={day === selected}
                  aria-label={`${fmt.day(day)}, ${live.length} planned ${live.length === 1 ? 'post' : 'posts'}`}
                  onPointerDown={(e) => e.button === 0 && setSelected(day)}
                  onClick={() => setSelected(day)}
                >
                  <span className="cal-num">{Number(day.slice(8))}</span>
                  <span className="cal-chips">
                    {posts.slice(0, 2).map((p) => (
                      <span key={p.id} className={`cal-chip ${deliveryOf(p)}`}
                        title={DELIVERY[deliveryOf(p)].label}>
                        {deliveryOf(p) === 'published' && <span aria-hidden="true">✓ </span>}
                        {deliveryOf(p) === 'failed' && <span aria-hidden="true">! </span>}
                        <b>{fmt.time(p.scheduled_at, tz)}</b> {headline(p.content, p.original_filename)}
                      </span>
                    ))}
                    {posts.length > 2 && <span className="cal-more">{posts.length - 2} more</span>}
                  </span>
                  {posts.length > 0 && (
                    <span className={`cal-dot ${dayDelivery(posts)}`} aria-hidden="true" />
                  )}
                </button>
              );
            })}
          </div>
        </div>

        <ul className="cal-legend" aria-label="Key">
          {(['upcoming', 'published', 'failed', 'past'] as const).map((d) => (
            <li key={d}><span className={`cal-swatch ${d}`} aria-hidden="true" />{DELIVERY[d].label}</li>
          ))}
        </ul>

        <section className="agenda" aria-live="polite">
          <h3>{fmt.day(selected)}</h3>
          {agenda.length === 0 ? (
            <p className="quiet">
              Nothing planned. Open an approved draft in your Library and choose Schedule.
            </p>
          ) : (
            <ul className="rows">
              {agenda.map((post) => (
                <li key={post.id}>
                  <button type="button" className="row" onClick={() => setOpenId(post.id)}>
                    <Thumb jobId={post.job_id} kind={post.media_kind} />
                    <span className="row-main">
                      <span className="row-title" dir="auto">
                        {headline(post.content, post.original_filename)}
                      </span>
                      <span className="row-sub">{post.original_filename}</span>
                    </span>
                    <span className="row-side">
                      <time dateTime={post.scheduled_at}>{fmt.time(post.scheduled_at, tz)}</time>
                      <Badge tone={DELIVERY[deliveryOf(post)].tone}>{DELIVERY[deliveryOf(post)].label}</Badge>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <PostSheet post={open} tz={tz} onClose={() => setOpenId(null)} onChanged={load} />
    </div>
  );
}

function PostSheet({
  post: current, tz, onClose, onChanged,
}: { post: Post | null; tz: string; onClose: () => void; onChanged: () => Promise<void> }) {
  const post = useLast(current);
  return (
    <Sheet open={current !== null} onClose={onClose} title={post?.original_filename ?? ''}>
      {post && <PostDetail key={post.id} post={post} tz={tz} onClose={onClose} onChanged={onChanged} />}
    </Sheet>
  );
}

function PostDetail({
  post, tz, onClose, onChanged,
}: { post: Post; tz: string; onClose: () => void; onChanged: () => Promise<void> }) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const cancelled = post.status === 'cancelled';

  async function run(name: string, work: () => Promise<Post>, message: (p: Post) => string) {
    setBusy(name);
    setError('');
    try {
      const next = await work();
      toast(message(next));
      onClose();
      await onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong.');
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="job-detail">
      <div className="post-when">
        <Icon name="calendar" />
        <div>
          <strong className={cancelled ? 'struck' : ''}>{fmt.dateTime(post.scheduled_at, tz)}</strong>
          <span>
            {tzLabel(tz)}
            {post.timezone !== tz &&
              ` · ${fmt.dateTime(post.scheduled_at, post.timezone)} in ${tzLabel(post.timezone)}`}
          </span>
        </div>
        <Badge tone={DELIVERY[deliveryOf(post)].tone}>{DELIVERY[deliveryOf(post)].label}</Badge>
      </div>
      {deliveryOf(post) !== 'upcoming' && !cancelled && (
        <p className={`callout ${deliveryOf(post) === 'failed' ? 'callout-red' : ''}`}>
          {DELIVERY[deliveryOf(post)].note}
          {post.published_url && (
            <> <a href={post.published_url} target="_blank" rel="noreferrer noopener">View the post</a></>
          )}
        </p>
      )}

      {editing ? (
        <div className="panel">
          <h3>{cancelled ? 'Schedule again' : 'Reschedule'}</h3>
          <ScheduleForm
            initial={{ at: post.scheduled_at, timezone: post.timezone }}
            submitLabel="Save"
            busy={busy === 'save'}
            error={error}
            onCancel={() => { setEditing(false); setError(''); }}
            onSubmit={(value) =>
              run(
                'save',
                () => api<Post>('PATCH', `/scheduled-posts/${post.id}`, value),
                (p) => `Moved to ${fmt.dateTime(p.scheduled_at, p.timezone)}`,
              )
            }
          />
        </div>
      ) : (
        <>
          {error && <ErrorNote message={error} />}
          <div className="job-actions">
            <Button variant="primary" onClick={() => setEditing(true)}>
              {cancelled ? 'Schedule again…' : 'Reschedule…'}
            </Button>
            {!cancelled && (
              <ConfirmButton
                label="Cancel post" confirm="Confirm cancel" busy={busy === 'cancel'}
                onConfirm={() =>
                  run('cancel', () => api<Post>('DELETE', `/scheduled-posts/${post.id}`), () => 'Post cancelled')
                }
              />
            )}
          </div>
          {!cancelled && (
            <p className="footnote">A cancelled post stays on the calendar and can be scheduled again.</p>
          )}
        </>
      )}

      <PostPublishing postId={post.id} active={!cancelled} />
      <ContentView text={post.content} />
    </div>
  );
}
