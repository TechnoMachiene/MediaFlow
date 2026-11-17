import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api';
import { SECTION_LABELS, humanize, localTz } from '../lib';
import { useSession } from '../session';
import type { AnalyticsReport, Timing } from '../types';
import { Badge, ErrorNote, Icon, PageHeader, Segmented, Spinner } from '../ui';

/* Colors are the validated reference categorical slots 1-3 (dataviz skill, palette.md), checked
 * against this app's card surfaces: light #ffffff and dark #1c1c1e, all-pairs, both modes. Aqua
 * is under 3:1 on light, so every line is also direct-labeled and a table view is offered. */

type Range = '7' | '30' | '90';
const RANGES: { value: Range; label: string }[] = [
  { value: '7', label: 'Last 7 days' },
  { value: '30', label: 'Last 30 days' },
  { value: '90', label: 'Last 90 days' },
];

const SERIES = [
  { key: 'uploads', label: 'Uploads', color: 'var(--viz-1)' },
  { key: 'approved', label: 'Approved', color: 'var(--viz-2)' },
  { key: 'published', label: 'Published', color: 'var(--viz-3)' },
] as const;

const STAGES: { key: keyof AnalyticsReport['timings']; label: string; hint: string }[] = [
  { key: 'queue_wait', label: 'Waiting for a worker', hint: 'Upload to analysis start' },
  { key: 'analysis', label: 'Analysis', hint: 'Gemini writing the draft' },
  { key: 'review_wait', label: 'Waiting for review', hint: 'Draft ready to decision' },
  { key: 'publish_lag', label: 'Going live', hint: 'Calendar time to published' },
];

const isoDay = (d: Date) => d.toLocaleDateString('en-CA'); // YYYY-MM-DD in local time
const pct = (v: number | null) => (v === null ? '–' : `${Math.round(v * 100)}%`);
const num = new Intl.NumberFormat();

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '–';
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 36 * 3600) return `${(seconds / 3600).toFixed(1)} h`;
  return `${(seconds / 86400).toFixed(1)} d`;
}

export function Insights() {
  const { user } = useSession();
  const [range, setRange] = useState<Range>('30');
  const [report, setReport] = useState<AnalyticsReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    const end = new Date();
    const start = new Date(end);
    start.setDate(end.getDate() - Number(range) + 1);
    try {
      setReport(await api<AnalyticsReport>('GET',
        `/analytics?start=${isoDay(start)}&end=${isoDay(end)}&timezone=${encodeURIComponent(localTz)}`));
      setError('');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load the report.');
    } finally {
      setLoading(false);
    }
  }, [range]);
  useEffect(() => { void load(); }, [load]);

  return (
    <div className="page">
      <PageHeader title="Insights" subtitle={`How content moves through ${user.workspace.name}`} />
      <div className="page-body">
        <div className="toolbar">
          <Segmented label="Period" value={range} onChange={setRange} options={RANGES} />
        </div>
        {error && <ErrorNote message={error} onRetry={load} />}
        {!report && !error && <div className="center"><Spinner /></div>}
        {report && (
          // Refetch keeps the frame: the previous report stays, dimmed, while the next loads.
          <div className={`insights viz-root ${loading ? 'refetching' : ''}`} aria-busy={loading}>
            <Kpis report={report} />
            <Findings report={report} />
            <section className="card" aria-labelledby="volume-title">
              <h2 id="volume-title" className="card-title">Content volume per day</h2>
              <VolumeChart report={report} />
            </section>
            <section className="card" aria-labelledby="stages-title">
              <h2 id="stages-title" className="card-title">Where the time goes</h2>
              <p className="quiet">Median time each draft spends in a stage; slowest first.</p>
              <StageBars report={report} />
            </section>
            <Breakdowns report={report} />
          </div>
        )}
      </div>
    </div>
  );
}

/* ── KPI row ─────────────────────────────────────────────────────────────── */

function Delta({ now, before, kind, upIsGood = true }: {
  now: number | null; before: number | null; kind: 'count' | 'rate'; upIsGood?: boolean;
}) {
  if (now === null || before === null) return null;
  const diff = kind === 'rate' ? Math.round((now - before) * 100) : now - before;
  if (diff === 0) return <span className="stat-delta">Same as last period</span>;
  const good = diff > 0 === upIsGood;
  const text = kind === 'rate' ? `${diff > 0 ? '+' : ''}${diff} pts` : `${diff > 0 ? '+' : ''}${num.format(diff)}`;
  return (
    <span className={`stat-delta ${good ? 'delta-good' : 'delta-bad'}`}>
      <span aria-hidden="true">{diff > 0 ? '▲' : '▼'}</span> {text} vs last period
    </span>
  );
}

function Kpis({ report }: { report: AnalyticsReport }) {
  const { totals: t, previous_totals: pt, rates: r, previous_rates: pr, timings } = report;
  const tiles: { label: string; value: string; delta?: React.ReactNode; hint?: string }[] = [
    { label: 'Uploads', value: num.format(t.uploads),
      delta: <Delta now={t.uploads} before={pt.uploads} kind="count" /> },
    { label: 'Approval rate', value: pct(r.approval),
      delta: <Delta now={r.approval} before={pr.approval} kind="rate" />,
      hint: `${t.approved} approved · ${t.rejected} rejected` },
    { label: 'Approved as written', value: pct(r.first_pass_approval),
      delta: <Delta now={r.first_pass_approval} before={pr.first_pass_approval} kind="rate" />,
      hint: 'No rejection or edit first' },
    { label: 'Median review wait', value: duration(timings.review_wait.p50),
      hint: timings.review_wait.count ? `p90 ${duration(timings.review_wait.p90)}` : 'No decisions yet' },
    { label: 'Published', value: num.format(t.published),
      delta: <Delta now={t.published} before={pt.published} kind="count" />,
      hint: t.publish_failed ? `${t.publish_failed} failed` : undefined },
  ];
  return (
    <div className="stat-row">
      {tiles.map((tile) => (
        <div key={tile.label} className="stat-tile">
          <span className="stat-label">{tile.label}</span>
          <span className="stat-value">{tile.value}</span>
          {tile.delta}
          {tile.hint && <span className="stat-hint">{tile.hint}</span>}
        </div>
      ))}
    </div>
  );
}

function Findings({ report }: { report: AnalyticsReport }) {
  if (!report.insights.length) {
    return <p className="quiet">Nothing stands out in this period.</p>;
  }
  return (
    <section aria-labelledby="findings-title">
      <h2 id="findings-title" className="section-title">What to look at</h2>
      <ul className="findings">
        {report.insights.map((i) => (
          <li key={i.kind + i.text} className={`finding finding-${i.severity}`}>
            <Icon name={i.severity === 'warn' ? 'alert' : 'activity'} />
            <span>
              <strong>{i.severity === 'warn' ? 'Needs attention: ' : 'Note: '}</strong>
              {i.text}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/* ── Volume: multi-line chart with crosshair, legend, end labels, table view ── */

const H = 220;
const PAD = { top: 12, right: 92, bottom: 26, left: 36 };

/** Draw at the container's real width, so text stays at its set size on any screen. */
function useWidth(fallback: number): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width) setWidth(Math.max(300, Math.round(entry.contentRect.width)));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

function niceMax(v: number) {
  if (v <= 4) return 4;
  const mag = 10 ** Math.floor(Math.log10(v));
  return Math.ceil(v / mag) * mag;
}

export function VolumeChart({ report }: { report: AnalyticsReport }) {
  const days = report.series;
  const [hover, setHover] = useState<number | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const [box, W] = useWidth(720);
  const top = niceMax(Math.max(1, ...days.flatMap((d) => SERIES.map((s) => d[s.key]))));
  const x = (i: number) => PAD.left + (days.length < 2 ? 0 : (i * (W - PAD.left - PAD.right)) / (days.length - 1));
  const y = (v: number) => PAD.top + (1 - v / top) * (H - PAD.top - PAD.bottom);
  const ticks = [0, top / 2, top];
  const every = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(W / 110))));
  const fmtDay = (iso: string) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const box = svg.current!.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * W;
    const step = days.length < 2 ? 1 : (W - PAD.left - PAD.right) / (days.length - 1);
    setHover(Math.max(0, Math.min(days.length - 1, Math.round((px - PAD.left) / step))));
  };
  const last = days[days.length - 1];

  return (
    <>
      <ul className="legend" aria-label="Series">
        {SERIES.map((s) => (
          <li key={s.key}><span className="legend-line" style={{ background: s.color }} />{s.label}</li>
        ))}
      </ul>
      <div className="chart" ref={box}>
        <svg
          ref={svg} viewBox={`0 0 ${W} ${H}`} role="img"
          aria-label={`Daily uploads, approvals and publications, ${days.length} days. The table below lists every value.`}
          onPointerMove={onMove} onPointerLeave={() => setHover(null)}
        >
          {ticks.map((t) => (
            <g key={t}>
              <line className="grid" x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} />
              <text className="tick" x={PAD.left - 6} y={y(t)} dy="0.32em" textAnchor="end">{num.format(t)}</text>
            </g>
          ))}
          {days.map((d, i) => i % every === 0 && (
            <text key={d.date} className="tick" x={x(i)} y={H - 6} textAnchor="middle">{fmtDay(String(d.date))}</text>
          ))}
          {SERIES.map((s) => (
            <polyline
              key={s.key} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round"
              points={days.map((d, i) => `${x(i)},${y(d[s.key])}`).join(' ')}
            />
          ))}
          {last && <EndLabels last={last} x={x(days.length - 1)} y={y} />}
          {hover !== null && (
            <g>
              <line className="crosshair" x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={H - PAD.bottom} />
              {SERIES.map((s) => (
                <circle key={s.key} cx={x(hover)} cy={y(days[hover][s.key])} r={4} fill={s.color}
                  stroke="var(--surface)" strokeWidth={2} />
              ))}
            </g>
          )}
        </svg>
        {hover !== null && (
          <div className="tooltip" role="status"
            style={{ left: `${(x(hover) / W) * 100}%` }}>
            <span className="tooltip-title">{fmtDay(String(days[hover].date))}</span>
            {SERIES.map((s) => (
              <span key={s.key} className="tooltip-row">
                <span className="legend-line" style={{ background: s.color }} />
                <strong>{num.format(days[hover][s.key])}</strong> {s.label}
              </span>
            ))}
          </div>
        )}
      </div>
      <details className="table-view">
        <summary>Show as a table</summary>
        <table>
          <thead>
            <tr><th scope="col">Day</th><th scope="col">Uploads</th><th scope="col">Drafted</th>
              <th scope="col">Approved</th><th scope="col">Rejected</th><th scope="col">Published</th></tr>
          </thead>
          <tbody>
            {days.map((d) => (
              <tr key={d.date}><th scope="row">{fmtDay(String(d.date))}</th><td>{d.uploads}</td>
                <td>{d.drafted}</td><td>{d.approved}</td><td>{d.rejected}</td><td>{d.published}</td></tr>
            ))}
          </tbody>
        </table>
      </details>
    </>
  );
}

function EndLabels({ last, x, y }: {
  last: AnalyticsReport['series'][number]; x: number; y: (v: number) => number;
}) {
  // Sorted by height and kept at least 13px apart, each tied to its line by a short leader.
  const placed = SERIES.map((s) => ({ ...s, value: last[s.key], at: y(last[s.key]) }))
    .sort((a, b) => a.at - b.at);
  for (let i = 1; i < placed.length; i++) {
    placed[i].at = Math.max(placed[i].at, placed[i - 1].at + 13);
  }
  return (
    <g>
      {placed.map((p) => (
        <g key={p.key}>
          <line className="leader" x1={x + 2} x2={x + 10} y1={y(p.value)} y2={p.at} />
          <text className="end-label" x={x + 12} y={p.at} dy="0.32em">{p.label} {num.format(p.value)}</text>
        </g>
      ))}
    </g>
  );
}

/* ── Where the time goes: ranked single-hue bars, value at the tip ────────── */

export function StageBars({ report }: { report: AnalyticsReport }) {
  const rows = STAGES.map((s) => ({ ...s, t: report.timings[s.key] as Timing }))
    .filter((r) => r.t.count > 0)
    .sort((a, b) => (b.t.p50 ?? 0) - (a.t.p50 ?? 0));
  if (!rows.length) return <p className="quiet">No timed drafts in this period yet.</p>;
  const top = Math.max(...rows.map((r) => r.t.p50 ?? 0), 1);
  return (
    <ul className="stage-bars">
      {rows.map((r) => (
        <li key={r.key} tabIndex={0}
          aria-label={`${r.label}: median ${duration(r.t.p50)}, p90 ${duration(r.t.p90)}, ${r.t.count} drafts`}>
          <span className="stage-name">{r.label}<small>{r.hint}</small></span>
          <span className="stage-track">
            <span className="stage-bar" style={{ width: `${Math.max(2, ((r.t.p50 ?? 0) / top) * 100)}%` }} />
            <span className="stage-value">{duration(r.t.p50)}</span>
          </span>
          <span className="stage-tip" role="tooltip">
            <strong>{duration(r.t.p50)}</strong> median · p90 {duration(r.t.p90)} · {r.t.count} drafts
          </span>
        </li>
      ))}
    </ul>
  );
}

/* ── Breakdowns ──────────────────────────────────────────────────────────── */

function Breakdowns({ report }: { report: AnalyticsReport }) {
  const channels = report.by_channel;
  return (
    <div className="breakdowns">
      <section className="card" aria-labelledby="kind-title">
        <h2 id="kind-title" className="card-title">By media</h2>
        <table>
          <thead><tr><th scope="col">Media</th><th scope="col">Uploads</th><th scope="col">Approval</th>
            <th scope="col">Median analysis</th><th scope="col">Median review wait</th></tr></thead>
          <tbody>
            {report.by_kind.map((k) => (
              <tr key={k.kind}>
                <th scope="row">{k.kind === 'image' ? 'Images' : 'Videos'}</th>
                <td>{num.format(k.uploads)}</td>
                <td>{pct(k.approved + k.rejected ? k.approved / (k.approved + k.rejected) : null)}</td>
                <td>{duration(k.analysis.p50)}</td>
                <td>{duration(k.review_wait.p50)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="card" aria-labelledby="channel-title">
        <h2 id="channel-title" className="card-title">By channel</h2>
        {channels.length === 0 ? <p className="quiet">Nothing was published in this period.</p> : (
          <table>
            <thead><tr><th scope="col">Channel</th><th scope="col">Published</th><th scope="col">Failed</th>
              <th scope="col">Success</th><th scope="col">Median lag</th></tr></thead>
            <tbody>
              {channels.map((c) => (
                <tr key={c.channel + c.provider}>
                  <th scope="row">
                    {SECTION_LABELS[c.channel] ?? humanize(c.channel)}
                    {c.provider === 'mock' && <> <Badge tone="neutral">Sandbox</Badge></>}
                  </th>
                  <td>{num.format(c.published)}</td>
                  <td>
                    {num.format(c.failed)}
                    {c.top_error && <small className="quiet"> {humanize(c.top_error).toLowerCase()}</small>}
                  </td>
                  <td>
                    <span className="meter" aria-hidden="true">
                      <span style={{ width: `${(c.success_rate ?? 0) * 100}%` }} />
                    </span>
                    {pct(c.success_rate)}
                  </td>
                  <td>{duration(c.publish_lag.p50)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
