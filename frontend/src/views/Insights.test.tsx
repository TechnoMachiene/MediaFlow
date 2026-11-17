import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { SessionContext } from '../session';
import { fakeServer } from '../test/server';
import type { AnalyticsReport, Me, Timing } from '../types';
import { Insights, duration } from './Insights';

const ws = { id: 'ws-a', name: 'Acme', role: 'manager' as const };
const me: Me = {
  id: 'u1', email: 'max@example.com', role: 'manager', created_at: '2026-01-01T00:00:00Z',
  workspace: ws, workspaces: [ws], platform_admin: false,
};
const t = (p50: number | null, count = 5): Timing => ({ count: p50 === null ? 0 : count, p50, p90: p50 && p50 * 2, max: p50 });
const totals = (over = {}) => ({
  uploads: 12, drafted: 11, failed: 1, approved: 6, rejected: 2, edits: 3, comments: 4,
  scheduled: 5, published: 4, publish_failed: 1, ...over,
});
const report = (over: Partial<AnalyticsReport> = {}): AnalyticsReport => ({
  start: '2026-03-01', end: '2026-03-03', timezone: 'UTC',
  totals: totals(), previous_totals: totals({ uploads: 8, published: 4 }),
  rates: { approval: 0.75, first_pass_approval: 0.5, failure: 0.08, publish_success: 0.8 },
  previous_rates: { approval: 0.6, first_pass_approval: 0.5, failure: 0, publish_success: null },
  timings: { queue_wait: t(4), analysis: t(40), review_wait: t(7200), publish_lag: t(null) },
  by_kind: [
    { kind: 'image', uploads: 10, drafted: 9, approved: 5, rejected: 1, analysis: t(30), review_wait: t(3600) },
    { kind: 'video', uploads: 2, drafted: 2, approved: 1, rejected: 1, analysis: t(120), review_wait: t(9000) },
  ],
  by_channel: [{ channel: 'instagram', provider: 'mock', requested: 5, published: 4, failed: 1,
    success_rate: 0.8, top_error: 'rate_limited', publish_lag: t(30) }],
  series: [
    { date: '2026-03-01', uploads: 2, drafted: 2, approved: 1, rejected: 0, published: 0 },
    { date: '2026-03-02', uploads: 6, drafted: 5, approved: 3, rejected: 1, published: 2 },
    { date: '2026-03-03', uploads: 4, drafted: 4, approved: 2, rejected: 1, published: 2 },
  ],
  insights: [{ kind: 'bottleneck', severity: 'warn',
    text: 'Most of a draft’s time is spent waiting for review (median 2.0 h), against 40 s being analyzed.' }],
  ...over,
});

function renderInsights() {
  render(
    <SessionContext.Provider value={{ user: me, signOut: () => {}, switchWorkspace: () => {} }}>
      <Insights />
    </SessionContext.Provider>,
  );
}

describe('Insights', () => {
  it('leads with the headline numbers and what to act on', async () => {
    fakeServer({ 'GET /analytics': { body: report() } });
    renderInsights();
    expect(await screen.findByText('75%')).toBeInTheDocument(); // approval rate
    expect(screen.getByText('+15 pts vs last period', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('+4 vs last period', { exact: false })).toBeInTheDocument(); // uploads 12 vs 8
    const reviewTile = screen.getByText('Median review wait', { selector: '.stat-label' }).parentElement!;
    expect(reviewTile).toHaveTextContent('2.0 h');
    expect(reviewTile).toHaveTextContent('p90 4.0 h');
    expect(screen.getByText(/Needs attention/).closest('li')).toHaveTextContent('waiting for review');
  });

  it('asks for the chosen period in the viewer’s timezone', async () => {
    const { calls } = fakeServer({ 'GET /analytics': { body: report() } });
    renderInsights();
    await screen.findByText('75%');
    await userEvent.click(screen.getByRole('radio', { name: 'Last 7 days' }));
    const paths = calls.map((c) => c.path).filter((p) => p.startsWith('/analytics'));
    const last = new URLSearchParams(paths[paths.length - 1].split('?')[1]);
    const days = (Date.parse(last.get('end')!) - Date.parse(last.get('start')!)) / 86400000;
    expect(days).toBe(6);
    expect(last.get('timezone')).toBeTruthy();
  });

  it('draws the volume chart with a legend, end labels, a crosshair and a table', async () => {
    fakeServer({ 'GET /analytics': { body: report() } });
    renderInsights();
    const legend = await screen.findByRole('list', { name: 'Series' });
    expect(within(legend).getAllByRole('listitem').map((l) => l.textContent)).toEqual(['Uploads', 'Approved', 'Published']);
    expect(screen.getByText('Uploads 4')).toBeInTheDocument(); // end label = last day
    const svg = screen.getByRole('img', { name: /Daily uploads/ });
    svg.getBoundingClientRect = () => ({ left: 0, width: 720, top: 0, height: 220, right: 720, bottom: 220, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.pointerMove(svg, { clientX: 340 });
    const tip = screen.getByRole('status');
    expect(tip).toHaveTextContent('6 Uploads');
    expect(tip).toHaveTextContent('3 Approved');
    expect(screen.getAllByRole('row')).toHaveLength(4 + 3 + 2); // day table, media, channel (+heads)
  });

  it('ranks the stages slowest first and skips stages with no data', async () => {
    fakeServer({ 'GET /analytics': { body: report() } });
    renderInsights();
    await screen.findByText('75%');
    const stages = screen.getAllByLabelText(/median/).map((el) => el.getAttribute('aria-label')!.split(':')[0]);
    expect(stages).toEqual(['Waiting for review', 'Analysis', 'Waiting for a worker']);
  });

  it('says so when nothing stands out', async () => {
    fakeServer({ 'GET /analytics': { body: report({ insights: [], by_channel: [] }) } });
    renderInsights();
    expect(await screen.findByText('Nothing stands out in this period.')).toBeInTheDocument();
    expect(screen.getByText('Nothing was published in this period.')).toBeInTheDocument();
  });
});

describe('duration', () => {
  it('reads like a person would say it', () => {
    expect([null, 42, 600, 7200, 3 * 86400].map(duration)).toEqual(['–', '42 s', '10 min', '2.0 h', '3.0 d']);
  });
});
