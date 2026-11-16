import type { Job, Role } from './types';

/* ── Motion ──────────────────────────────────────────────────────────────── */

/** Apple-style spring: `response` in seconds, `damping` ratio (1 = no overshoot). */
export function spring(response = 0.35, damping = 1, velocity = 0) {
  return {
    type: 'spring' as const,
    stiffness: (2 * Math.PI / response) ** 2,
    damping: (4 * Math.PI * damping) / response,
    mass: 1,
    velocity,
  };
}

/** Where a flick comes to rest, using scroll-style exponential decay. Velocity in px/s. */
export function project(velocity: number, deceleration = 0.998) {
  return ((velocity / 1000) * deceleration) / (1 - deceleration);
}

export function rubberband(overshoot: number, dimension: number, constant = 0.55) {
  return (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot));
}

/* ── Roles ───────────────────────────────────────────────────────────────── */

export const isAdmin = (role: Role) => role === 'admin' || role === 'super_admin';
export const isReviewer = (role: Role) => role === 'manager' || isAdmin(role);
export const roleLabel: Record<Role, string> = {
  user: 'Member',
  manager: 'Manager',
  admin: 'Admin',
  super_admin: 'Owner',
};

/* ── Job state ───────────────────────────────────────────────────────────── */

export type Tone = 'neutral' | 'blue' | 'green' | 'orange' | 'red';
export interface JobState {
  key: 'progress' | 'review' | 'approved' | 'rejected' | 'stopped';
  label: string;
  tone: Tone;
  active: boolean;
  note: string;
}

export const CANCELLABLE = new Set(['queued', 'waiting', 'retrying']);

/** Collapse the queue status and the approval status into the one state a person cares about. */
export function jobState(job: Pick<Job, 'status' | 'approval_status'>): JobState {
  const s = (key: JobState['key'], label: string, tone: Tone, note: string, active = false) => ({
    key, label, tone, note, active,
  });
  switch (job.status) {
    case 'done':
      if (job.approval_status === 'approved')
        return s('approved', 'Approved', 'green', 'Approved and ready to schedule.');
      if (job.approval_status === 'rejected')
        return s('rejected', 'Rejected', 'red', 'A reviewer rejected this draft.');
      return s('review', 'In review', 'orange', 'Drafts are ready and waiting for a reviewer.');
    case 'queued':
      return s('progress', 'Queued', 'neutral', 'Waiting for a worker to pick this up.', true);
    case 'waiting':
      return s('progress', 'Queued', 'neutral', 'Identical media is already being analyzed; this will reuse its result.', true);
    case 'processing':
      return s('progress', 'Analyzing', 'blue', 'Gemini is analyzing this media.', true);
    case 'retrying':
      return s('progress', 'Retrying', 'orange', 'The last attempt failed. It will be retried automatically.', true);
    case 'failed':
    case 'dead':
      return s('stopped', 'Failed', 'red', 'Analysis failed and will not be retried.');
    case 'cancelled':
      return s('stopped', 'Cancelled', 'neutral', 'This job was cancelled before it ran.');
    case 'expired':
      return s('stopped', 'Expired', 'neutral', 'This job expired before a result was saved.');
    default:
      return s('stopped', job.status, 'neutral', '');
  }
}

/* ── Generated content ───────────────────────────────────────────────────── */

export type Content = { structured: Record<string, Record<string, unknown>> } | { raw: string };

/** Gemini is asked for a JSON object keyed by platform; fall back to plain text if it isn't. */
export function parseContent(text: string): Content {
  const trimmed = text.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '');
  try {
    const value: unknown = JSON.parse(trimmed);
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const sections = Object.entries(value).filter(
        ([, v]) => v && typeof v === 'object' && !Array.isArray(v),
      );
      if (sections.length) {
        return { structured: Object.fromEntries(sections) as Record<string, Record<string, unknown>> };
      }
    }
  } catch {
    /* not JSON */
  }
  return { raw: text };
}

export const SECTION_LABELS: Record<string, string> = {
  instagram: 'Instagram', tiktok: 'TikTok', facebook: 'Facebook', content_summary: 'Summary',
};
export const FIELD_LABELS: Record<string, string> = {
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
export const humanize = (key: string) =>
  key.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

/** "instagram.caption" → "Instagram · Caption"; a bare key is a plain-text draft's only field. */
export function fieldLabel(path: string): string {
  const [section, field] = path.split('.');
  if (!field) return 'Draft';
  return `${SECTION_LABELS[section] ?? humanize(section)} · ${FIELD_LABELS[field] ?? humanize(field)}`;
}

/** Every field a comment can be anchored to, in display order. */
export function fieldPaths(text: string): string[] {
  const content = parseContent(text);
  if ('raw' in content) return [];
  return Object.entries(content.structured).flatMap(([section, fields]) =>
    Object.keys(fields).map((field) => `${section}.${field}`));
}

const asText = (v: unknown): string =>
  v == null ? '' : Array.isArray(v) ? v.map(String).join('\n') : typeof v === 'object'
    ? JSON.stringify(v) : String(v);

export interface FieldChange { path: string; before: string; after: string }

/** What changed between two versions, field by field (added and removed fields included). */
export function diffContent(before: string, after: string): FieldChange[] {
  const a = parseContent(before);
  const b = parseContent(after);
  if ('raw' in a || 'raw' in b) {
    const x = 'raw' in a ? a.raw : before;
    const y = 'raw' in b ? b.raw : after;
    return x === y ? [] : [{ path: 'draft', before: x, after: y }];
  }
  const changes: FieldChange[] = [];
  const sections = [...new Set([...Object.keys(a.structured), ...Object.keys(b.structured)])];
  for (const section of sections) {
    const left = a.structured[section] ?? {};
    const right = b.structured[section] ?? {};
    for (const field of [...new Set([...Object.keys(left), ...Object.keys(right)])]) {
      const x = asText(left[field]);
      const y = asText(right[field]);
      if (x !== y) changes.push({ path: `${section}.${field}`, before: x, after: y });
    }
  }
  return changes;
}

const str = (v: unknown) => (typeof v === 'string' ? v.trim() : '');

/** One line that says what this media is about, for tiles and calendar entries. */
export function headline(text: string | undefined | null, fallback: string): string {
  if (!text) return fallback;
  const content = parseContent(text);
  if ('raw' in content) return content.raw.split('\n')[0].slice(0, 140) || fallback;
  const c = content.structured;
  return (
    str(c.content_summary?.main_message) ||
    str(c.instagram?.cover_text) ||
    str(c.instagram?.caption) ||
    str(c.facebook?.post_copy) ||
    fallback
  );
}

/* ── Time ────────────────────────────────────────────────────────────────── */

export const localTz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

export const timezones: string[] = (() => {
  const supported = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
    .supportedValuesOf;
  const list = new Set<string>(supported ? supported('timeZone') : []);
  list.add(localTz);
  list.add('UTC');
  return [...list].sort();
})();

/** API timestamps without an offset are UTC. */
export function parseDate(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : value + 'Z');
}

const pad = (n: number) => String(n).padStart(2, '0');

function parts(date: Date, tz: string) {
  const out: Record<string, number> = {};
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hourCycle: 'h23',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  for (const p of fmt.formatToParts(date)) if (p.type !== 'literal') out[p.type] = Number(p.value);
  return out;
}

/** The wall-clock date and time of an instant in a timezone. */
export function wallClock(date: Date, tz: string) {
  const p = parts(date, tz);
  return {
    date: `${p.year}-${pad(p.month)}-${pad(p.day)}`,
    time: `${pad(p.hour % 24)}:${pad(p.minute)}`,
  };
}

/** The instant at which a timezone's wall clock reads `date` `time`. */
export function zonedToInstant(date: string, time: string, tz: string): Date {
  const [y, m, d] = date.split('-').map(Number);
  const [hh, mm] = time.split(':').map(Number);
  const guess = Date.UTC(y, m - 1, d, hh, mm);
  const offsetAt = (ms: number) => {
    const p = parts(new Date(ms), tz);
    return Date.UTC(p.year, p.month - 1, p.day, p.hour % 24, p.minute, p.second) - ms;
  };
  let instant = guess - offsetAt(guess);
  instant = guess - offsetAt(instant); // settle across DST boundaries
  return new Date(instant);
}

export const todayIn = (tz: string) => wallClock(new Date(), tz).date;

export function addDays(ymd: string, days: number): string {
  const d = new Date(ymd + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function addMonths(month: string, n: number): string {
  const [y, m] = month.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1 + n, 1)).toISOString().slice(0, 7);
}

export const weekStart: number = (() => {
  try {
    const locale = new Intl.Locale(navigator.language) as Intl.Locale & {
      getWeekInfo?: () => { firstDay: number };
      weekInfo?: { firstDay: number };
    };
    return (locale.getWeekInfo?.() ?? locale.weekInfo)?.firstDay ?? 1;
  } catch {
    return 1;
  }
})() % 7;

/** The 42 days (six weeks) shown for a month. */
export function monthGrid(month: string): string[] {
  const first = `${month}-01`;
  const lead = (new Date(first + 'T00:00:00Z').getUTCDay() - weekStart + 7) % 7;
  return Array.from({ length: 42 }, (_, i) => addDays(first, i - lead));
}

const utcDay = (ymd: string) => new Date(ymd + 'T12:00:00Z');

export const fmt = {
  time: (iso: string, tz: string) =>
    new Intl.DateTimeFormat(undefined, { timeZone: tz, hour: 'numeric', minute: '2-digit' })
      .format(parseDate(iso)),
  dateTime: (iso: string, tz: string) =>
    new Intl.DateTimeFormat(undefined, {
      timeZone: tz, weekday: 'short', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    }).format(parseDate(iso)),
  date: (iso: string) =>
    new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
      .format(parseDate(iso)),
  month: (month: string) =>
    new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', month: 'long', year: 'numeric' })
      .format(utcDay(`${month}-01`)),
  day: (ymd: string) =>
    new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', weekday: 'long', month: 'long', day: 'numeric' })
      .format(utcDay(ymd)),
  weekday: (ymd: string) =>
    new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', weekday: 'short' }).format(utcDay(ymd)),
  ago(iso: string) {
    const seconds = (parseDate(iso).getTime() - Date.now()) / 1000;
    const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
    const abs = Math.abs(seconds);
    if (abs < 45) return 'just now';
    if (abs < 3600) return rtf.format(Math.round(seconds / 60), 'minute');
    if (abs < 86400) return rtf.format(Math.round(seconds / 3600), 'hour');
    if (abs < 86400 * 7) return rtf.format(Math.round(seconds / 86400), 'day');
    return fmt.date(iso);
  },
  bytes(n: number) {
    if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
    if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 ** 3).toFixed(2)} GB`;
  },
};

export const tzLabel = (tz: string) => tz.replace(/_/g, ' ');
