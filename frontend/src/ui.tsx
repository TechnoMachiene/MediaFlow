import { motion } from 'motion/react';
import {
  useEffect, useId, useRef, useState,
  type ButtonHTMLAttributes, type ReactNode,
} from 'react';
import { mediaUrl } from './api';
import { jobState, roleLabel, spring, type Tone } from './lib';
import { useSession } from './session';
import type { Job, MediaKind } from './types';

/* ── Icons ───────────────────────────────────────────────────────────────── */

const PATHS = {
  library: <><rect x="3.5" y="6.5" width="13" height="13" rx="3" /><path d="M7.5 3.5h9a4 4 0 0 1 4 4v9" /><path d="m4 16 3.5-3.5 3 3 2-2 4 4" /></>,
  review: <><path d="M12 3.5 5 6v5.5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9V6z" /><path d="m9 12 2.2 2.2L15.2 10" /></>,
  calendar: <><rect x="3.5" y="5" width="17" height="15.5" rx="3.5" /><path d="M3.5 10h17M8 3v4M16 3v4" /></>,
  team: <><circle cx="9" cy="8.5" r="3.5" /><path d="M2.5 20c.6-3.4 3.2-5.5 6.5-5.5s5.9 2.1 6.5 5.5" /><path d="M15.5 5.200a3.5 3.5 0 0 1 0 6.600M18 14.900c1.9.8 3.1 2.6 3.5 5.1" /></>,
  activity: <path d="M3 12.500h4l2.5-7 4.5 13 2.5-6H21" />,
  plus: <path d="M12 5v14M5 12h14" />,
  close: <path d="m6.5 6.5 11 11M17.5 6.500l-11 11" />,
  check: <path d="m5 12.5 4.5 4.500L19 7.5" />,
  alert: <><circle cx="12" cy="12" r="9" /><path d="M12 7.500v5.500M12 16.300v.2" /></>,
  film: <><rect x="3.5" y="5" width="17" height="14" rx="3" /><path d="m10 9.5 5 2.5-5 2.500z" /></>,
  photo: <><rect x="3.5" y="5" width="17" height="14" rx="3" /><circle cx="8.5" cy="10" r="1.5" /><path d="m4 17 4.5-4.5 3.5 3.5 2.5-2.5 5.5 5.5" /></>,
  copy: <><rect x="8.5" y="8.5" width="11" height="11" rx="2.5" /><path d="M15.5 5.500v-1a1 1 0 0 0-1-1h-9a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h1" /></>,
  left: <path d="m14.5 5.5-6.5 6.5 6.5 6.5" />,
  right: <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />,
  upload: <><path d="M12 15.500V4M7.5 8.5 12 4l4.5 4.5" /><path d="M4.5 14.500v3a2.5 2.5 0 0 0 2.5 2.500h10a2.5 2.5 0 0 0 2.5-2.500v-3" /></>,
  person: <><circle cx="12" cy="8.5" r="3.8" /><path d="M4.5 20.500c.8-3.7 3.8-6 7.5-6s6.7 2.3 7.5 6" /></>,
  gear: <><circle cx="12" cy="12" r="3" /><path d="M12 2.5v3M12 18.5v3M21.5 12h-3M5.5 12h-3M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1M18.7 18.7l-2.1-2.1M7.4 7.4 5.3 5.3" /></>,
  lock: <><rect x="5" y="10.5" width="14" height="10" rx="3" /><path d="M8.5 10.500V8a3.5 3.5 0 0 1 7 0v2.5" /></>,
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name }: { name: IconName }) {
  return (
    <svg
      className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  );
}

export const Spinner = () => <span className="spinner" role="progressbar" aria-label="Loading" />;

/* ── Buttons ─────────────────────────────────────────────────────────────── */

type Variant = 'primary' | 'tinted' | 'gray' | 'danger';

export function Button({
  variant = 'gray', busy, children, className = '', ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; busy?: boolean }) {
  return (
    <button
      type="button"
      {...rest}
      className={`btn btn-${variant} ${className}`}
      disabled={rest.disabled || busy}
      aria-busy={busy || undefined}
    >
      {busy ? <Spinner /> : children}
    </button>
  );
}

/** A destructive action that asks once, in place, before it commits. */
export function ConfirmButton({
  label, confirm, onConfirm, busy,
}: { label: string; confirm: string; onConfirm: () => void; busy?: boolean }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(t);
  }, [armed]);
  return (
    <Button
      variant="danger"
      className={armed ? 'armed' : ''}
      busy={busy}
      onClick={() => (armed ? (setArmed(false), onConfirm()) : setArmed(true))}
      onBlur={() => setArmed(false)}
    >
      {armed ? confirm : label}
    </Button>
  );
}

export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(t);
  }, [copied]);
  return (
    <button
      type="button"
      className="copy-btn"
      onClick={() => navigator.clipboard?.writeText(text).then(() => setCopied(true), () => {})}
    >
      <Icon name={copied ? 'check' : 'copy'} />
      <span aria-live="polite">{copied ? 'Copied' : label}</span>
    </button>
  );
}

/* ── Segmented control ───────────────────────────────────────────────────── */

export function Segmented<T extends string>({
  value, onChange, options, label,
}: { value: T; onChange: (v: T) => void; options: { value: T; label: string }[]; label: string }) {
  const id = useId();
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          // Selection moves on press, not on release.
          onPointerDown={(e) => e.button === 0 && onChange(o.value)}
          onClick={() => onChange(o.value)}
        >
          {o.value === value && (
            <motion.span layoutId={id} className="segmented-thumb" transition={spring(0.3, 1)} />
          )}
          <span>{o.label}</span>
        </button>
      ))}
    </div>
  );
}

/* ── Status ──────────────────────────────────────────────────────────────── */

export function Badge({ tone, children, live }: { tone: Tone; children: ReactNode; live?: boolean }) {
  return (
    <span className={`badge badge-${tone}`}>
      {live && <span className="badge-pulse" aria-hidden="true" />}
      {children}
    </span>
  );
}

export function JobBadge({ job }: { job: Pick<Job, 'status' | 'approval_status'> }) {
  const state = jobState(job);
  return <Badge tone={state.tone} live={state.active}>{state.label}</Badge>;
}

/* ── Media ───────────────────────────────────────────────────────────────── */

/** Lazily loads the protected image for a job once it scrolls near the viewport. */
export function Thumb({ jobId, kind }: { jobId: string; kind: MediaKind }) {
  const ref = useRef<HTMLDivElement>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [shown, setShown] = useState(false);

  useEffect(() => {
    setUrl(null);
    setShown(false);
    if (kind !== 'image' || !ref.current) return;
    let alive = true;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        io.disconnect();
        mediaUrl(jobId).then((u) => alive && setUrl(u), () => {});
      },
      { rootMargin: '300px' },
    );
    io.observe(ref.current);
    return () => {
      alive = false;
      io.disconnect();
    };
  }, [jobId, kind]);

  return (
    <div ref={ref} className="thumb">
      <Icon name={kind === 'video' ? 'film' : 'photo'} />
      {url && (
        <img src={url} alt="" className={shown ? 'shown' : ''} onLoad={() => setShown(true)} />
      )}
    </div>
  );
}

/* ── Page furniture ──────────────────────────────────────────────────────── */

export function PageHeader({
  title, subtitle, children, onAccount,
}: { title: string; subtitle?: string; children?: ReactNode; onAccount?: () => void }) {
  return (
    <header className="page-header">
      <div className="page-title">
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      <div className="page-actions">
        {children}
        <AccountButton onClick={onAccount} />
      </div>
    </header>
  );
}

/** Phones have no sidebar, so the account lives in the header. */
function AccountButton({ onClick }: { onClick?: () => void }) {
  const { user } = useSession();
  return (
    <button
      type="button"
      className="icon-btn account-btn"
      aria-label={`Account: ${user.email}, ${roleLabel[user.role]}`}
      onClick={onClick ?? (() => window.dispatchEvent(new Event('mediaflow:account')))}
    >
      <Icon name="person" />
    </button>
  );
}

export function Empty({
  icon, title, children, action,
}: { icon: IconName; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-icon"><Icon name={icon} /></div>
      <h2>{title}</h2>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-note" role="alert">
      <Icon name="alert" />
      <span>{message}</span>
      {onRetry && <button type="button" className="link" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function Field({
  label, hint, error, children,
}: { label: string; hint?: string; error?: string; children: ReactNode }) {
  return (
    <label className={`field ${error ? 'field-invalid' : ''}`}>
      <span className="field-label">{label}</span>
      {children}
      {error ? <span className="field-error" role="alert">{error}</span>
        : hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  );
}

/** Keeps the last non-null value so a closing sheet still has something to show. */
export function useLast<T>(value: T | null): T | null {
  const ref = useRef(value);
  if (value !== null) ref.current = value;
  return ref.current;
}
