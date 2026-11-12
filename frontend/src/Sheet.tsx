import { animate, motion, useMotionValue, useReducedMotion, useTransform } from 'motion/react';
import {
  useEffect, useId, useLayoutEffect, useRef, useState,
  type PointerEvent as ReactPointerEvent, type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { project, rubberband, spring } from './lib';
import { Icon } from './ui';

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
}

/**
 * A modal sheet: rises from the bottom edge on phones, and to the centre on wider screens.
 * It leaves the way it arrived, can be dragged by its header, and can be grabbed mid-flight.
 */
export function Sheet(props: Props) {
  const [mounted, setMounted] = useState(props.open);
  if (props.open && !mounted) setMounted(true);
  if (!mounted) return null;
  return createPortal(<Panel {...props} onExited={() => setMounted(false)} />, document.body);
}

const pxSpring = (response: number, damping: number, velocity: number) => ({
  ...spring(response, damping, velocity),
  restDelta: 0.5,
  restSpeed: 10,
});

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), video[controls], [tabindex]:not([tabindex="-1"])';

function Panel({ open, onClose, onExited, title, children, footer }: Props & { onExited: () => void }) {
  const reduced = useReducedMotion();
  const titleId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const travel = useRef(window.innerHeight); // distance from resting to fully off-screen
  const y = useMotionValue(window.innerHeight);
  const fade = useMotionValue(reduced ? 0 : 1);
  const scrim = useTransform(y, (v) => 1 - Math.min(Math.max(v / travel.current, 0), 1));
  const running = useRef<{ stop(): void } | null>(null);
  const releaseVelocity = useRef(0);
  const entered = useRef(false);
  const exited = useRef(onExited);
  exited.current = onExited;
  const close = useRef(onClose);
  close.current = onClose;

  // Open and close are the same spring, re-targeted from wherever the sheet is right now.
  useLayoutEffect(() => {
    const el = panel.current!;
    travel.current = window.innerHeight - el.offsetTop;
    running.current?.stop();
    const velocity = releaseVelocity.current;
    releaseVelocity.current = 0;
    const done = () => exited.current();

    if (reduced) {
      y.set(0);
      running.current = animate(fade, open ? 1 : 0, {
        duration: 0.2, ease: 'easeOut', onComplete: open ? undefined : done,
      });
    } else if (open) {
      if (!entered.current) y.set(travel.current);
      running.current = animate(y, 0, pxSpring(0.4, 1, velocity));
    } else {
      running.current = animate(y, travel.current, {
        ...pxSpring(0.35, 1, velocity), onComplete: done,
      });
    }
    entered.current = true;
    return () => running.current?.stop();
  }, [open, reduced, y, fade]);

  // Modal housekeeping: lock page scroll, move focus in, give it back on the way out.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const root = document.documentElement;
    const { overflow, paddingRight } = root.style;
    // Reserve the scrollbar's width so the page behind doesn't shift sideways.
    root.style.paddingRight = `${window.innerWidth - root.clientWidth}px`;
    root.style.overflow = 'hidden';
    panel.current?.focus({ preventScroll: true });
    return () => {
      root.style.overflow = overflow;
      root.style.paddingRight = paddingRight;
      previous?.focus?.({ preventScroll: true });
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close.current();
      } else if (e.key === 'Tab' && panel.current) {
        const items = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
        if (!items.length) return e.preventDefault();
        const first = items[0];
        const last = items[items.length - 1];
        const active = document.activeElement;
        if (e.shiftKey && (active === first || active === panel.current)) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && active === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open]);

  /* ── Drag to dismiss ───────────────────────────────────────────────────── */
  const drag = useRef<{
    id: number; start: number; origin: number; active: boolean;
    samples: { t: number; y: number }[];
  } | null>(null);

  function onPointerDown(e: ReactPointerEvent<HTMLElement>) {
    if (reduced || !open || e.button !== 0) return;
    if ((e.target as HTMLElement).closest('button, a, input, select, textarea')) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    running.current?.stop(); // catch it mid-flight; `y` already holds the on-screen value
    drag.current = { id: e.pointerId, start: e.clientY, origin: y.get(), active: false, samples: [] };
  }

  function onPointerMove(e: ReactPointerEvent<HTMLElement>) {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    if (!d.active) {
      if (Math.abs(e.clientY - d.start) < 8) return; // hysteresis before committing to a drag
      d.active = true;
      d.start = e.clientY; // …then track 1:1 from here, with no jump
    }
    const raw = d.origin + e.clientY - d.start;
    y.set(raw < 0 ? -rubberband(-raw, window.innerHeight) : raw);
    const now = performance.now();
    d.samples.push({ t: now, y: e.clientY });
    while (d.samples.length > 2 && now - d.samples[0].t > 100) d.samples.shift();
  }

  function onPointerEnd(e: ReactPointerEvent<HTMLElement>) {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    drag.current = null;
    const first = d.samples[0];
    const last = d.samples[d.samples.length - 1];
    let velocity = 0;
    if (first && last && last.t > first.t && performance.now() - last.t < 80) {
      velocity = ((last.y - first.y) / (last.t - first.t)) * 1000;
    }
    // Decide from where the gesture is heading, not from where the finger let go.
    const landing = y.get() + project(velocity);
    const height = panel.current?.offsetHeight ?? window.innerHeight;
    if (e.type === 'pointerup' && d.active && landing > height / 2) {
      releaseVelocity.current = velocity;
      onClose();
    } else {
      running.current = animate(y, 0, pxSpring(0.35, Math.abs(velocity) > 300 ? 0.8 : 1, velocity));
    }
  }

  return (
    <motion.div className="sheet-root" style={{ opacity: fade }}>
      <motion.div className="sheet-scrim" style={{ opacity: scrim }} onClick={onClose} />
      <motion.div
        ref={panel}
        className="sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        style={{ y }}
      >
        <header
          className="sheet-header"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerEnd}
          onPointerCancel={onPointerEnd}
        >
          <span className="sheet-grabber" aria-hidden="true" />
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <Icon name="close" />
          </button>
        </header>
        <div className="sheet-body">{children}</div>
        {footer && <footer className="sheet-footer">{footer}</footer>}
      </motion.div>
    </motion.div>
  );
}
