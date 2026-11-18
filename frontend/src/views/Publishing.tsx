import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { useLive } from '../live';
import { ApiError, api } from '../api';
import { SECTION_LABELS, fmt, humanize, isAdmin, type Tone } from '../lib';
import { useSession } from '../session';
import { useToast } from '../toast';
import type {
  Connection, Publication, PublicationStatus, PublishingProvider, SandboxFeed,
} from '../types';
import { Badge, Button, ConfirmButton, Empty, ErrorNote, Field, PageHeader, Spinner } from '../ui';

const message = (err: unknown, fallback: string) => (err instanceof ApiError ? err.message : fallback);
const channelLabel = (c: string) => SECTION_LABELS[c] ?? humanize(c);

const BEHAVIOR_HINT: Record<string, string> = {
  succeed: 'Publishes straight away.',
  flaky: 'Fails twice with 503, then publishes: watch the retries and backoff.',
  rate_limited: 'Answers 429 once with Retry-After, then publishes.',
  processing: 'Accepts the post as processing and finishes it later, like video uploads.',
  reject: 'Refuses the post for policy: a permanent failure, not retried.',
  expire: 'Issues a token that has expired and refuses to refresh it: the account needs reconnecting.',
};

export const PUBLICATION_STATE: Record<PublicationStatus, { label: string; tone: Tone; live?: boolean }> = {
  queued: { label: 'Waiting for its time', tone: 'neutral' },
  publishing: { label: 'Sending', tone: 'blue', live: true },
  processing: { label: 'Platform is processing', tone: 'blue', live: true },
  retrying: { label: 'Retrying', tone: 'orange', live: true },
  published: { label: 'Published', tone: 'green' },
  failed: { label: 'Failed', tone: 'red' },
  unknown: { label: 'Needs a check', tone: 'orange' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
};

/* ── Admin page ──────────────────────────────────────────────────────────── */

export function Publishing() {
  const { user } = useSession();
  const [providers, setProviders] = useState<PublishingProvider[] | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [feed, setFeed] = useState<SandboxFeed | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const [p, c, f] = await Promise.all([
        api<PublishingProvider[]>('GET', '/publishing/providers'),
        api<Connection[]>('GET', '/publishing/connections'),
        api<SandboxFeed>('GET', '/publishing/sandbox'),
      ]);
      setProviders(p);
      setConnections(c);
      setFeed(f);
      setError('');
    } catch (err) {
      setError(message(err, 'Could not load publishing.'));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLive(['publishing', 'calendar'], load);

  return (
    <div className="page">
      <PageHeader title="Publishing" subtitle={`Accounts ${user.workspace.name} publishes to`} />
      <div className="page-body narrow">
        {error && <ErrorNote message={error} onRetry={load} />}
        {providers === null && !error && <div className="center"><Spinner /></div>}
        {providers?.length === 0 && (
          <Empty icon="upload" title="Publishing is switched off">
            This installation has no publishing providers enabled (PUBLISHING_PROVIDERS).
          </Empty>
        )}
        {providers && providers.length > 0 && (
          <>
            <Accounts connections={connections} onChanged={load} />
            {isAdmin(user.role) && providers.map((p) => <ConnectForm key={p.name} provider={p} />)}
            {feed && providers.some((p) => p.sandbox) && <SandboxView feed={feed} />}
          </>
        )}
      </div>
    </div>
  );
}

function Accounts({ connections, onChanged }: { connections: Connection[]; onChanged: () => void }) {
  const toast = useToast();
  const { user } = useSession();
  async function disconnect(c: Connection) {
    try {
      await api<Connection>('DELETE', `/publishing/connections/${c.id}`);
      toast(`${c.account_name} disconnected`);
      onChanged();
    } catch (err) {
      toast(message(err, 'Could not disconnect.'), 'error');
    }
  }
  return (
    <section aria-labelledby="accounts-title">
      <h2 id="accounts-title" className="section-title">Connected accounts</h2>
      {connections.length === 0 ? <p className="quiet">No accounts yet.</p> : (
        <ul className="rows">
          {connections.map((c) => (
            <li key={c.id} className="row">
              <span className="row-main">
                <span className="row-title">{c.account_name}</span>
                <span className="row-sub">
                  {c.provider_name} · {channelLabel(c.channel)}
                  {c.options.behavior && ` · simulates “${humanize(c.options.behavior)}”`}
                  {c.last_error && ` · ${c.last_error}`}
                </span>
              </span>
              <span className="row-side">
                {c.sandbox && <Badge tone="neutral">Sandbox</Badge>}
                {c.status !== 'active' && <Badge tone="orange">Reconnect</Badge>}
                {isAdmin(user.role) && (
                  <ConfirmButton label="Disconnect" confirm="Disconnect?" onConfirm={() => void disconnect(c)} />
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ConnectForm({ provider }: { provider: PublishingProvider }) {
  const [channel, setChannel] = useState(provider.channels[0]);
  const behaviors = provider.options.behavior ?? [];
  const [behavior, setBehavior] = useState(behaviors[0] ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function start(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const out = await api<{ authorize_url: string }>('POST', '/publishing/connections/authorize', {
        provider: provider.name, channel, options: behavior ? { behavior } : {},
      });
      window.location.assign(out.authorize_url); // the platform's consent screen
    } catch (err) {
      setError(message(err, 'Could not start connecting.'));
      setBusy(false);
    }
  }

  return (
    <form className="form panel" onSubmit={start}>
      <h3>Connect a {provider.display_name.toLowerCase()} account</h3>
      {provider.sandbox ? (
        <p className="quiet">
          The sandbox is a pretend platform inside MediaFlow. It runs the real flow (consent,
          tokens, delivery, retries) and nothing leaves this installation.
        </p>
      ) : (
        <p className="quiet">
          <strong>This posts to the real, public account.</strong> It uses the Page and token set
          in the server's .env file, which MediaFlow checks with Meta when you connect. Each
          workspace that should post to it connects it once.
        </p>
      )}
      <div className="form-row">
        <Field label="Posts the draft for">
          <select value={channel} onChange={(e) => setChannel(e.target.value)}>
            {provider.channels.map((c) => <option key={c} value={c}>{channelLabel(c)}</option>)}
          </select>
        </Field>
        {behaviors.length > 0 && (
          <Field label="Simulate" hint={BEHAVIOR_HINT[behavior]}>
            <select value={behavior} onChange={(e) => setBehavior(e.target.value)}>
              {behaviors.map((b) => <option key={b} value={b}>{humanize(b)}</option>)}
            </select>
          </Field>
        )}
      </div>
      {error && <ErrorNote message={error} />}
      <div className="form-actions">
        <span className="spacer" />
        <Button variant="primary" type="submit" busy={busy}>Connect…</Button>
      </div>
    </form>
  );
}

function SandboxView({ feed }: { feed: SandboxFeed }) {
  return (
    <section aria-labelledby="sandbox-title">
      <h2 id="sandbox-title" className="section-title">What the sandbox platform received</h2>
      {feed.posts.length === 0 ? <p className="quiet">No sandbox posts yet.</p> : (
        <ul className="rows" aria-label="Sandbox posts">
          {feed.posts.map((p) => (
            <li key={p.external_id} id={`sandbox-${p.external_id}`} className="row">
              <span className="row-main">
                <span className="row-title" dir="auto">{p.body}</span>
                <span className="row-sub">
                  {p.account_name} · {channelLabel(p.channel)} · {fmt.ago(p.created_at)}
                  {p.hashtags.length > 0 && ` · ${p.hashtags.join(' ')}`}
                </span>
              </span>
              <span className="row-side">
                <Badge tone={p.state === 'published' ? 'green' : 'blue'}>{humanize(p.state)}</Badge>
              </span>
            </li>
          ))}
        </ul>
      )}
      {feed.requests.length > 0 && (
        <details>
          <summary>Request log ({feed.requests.length})</summary>
          <ol className="request-log">
            {feed.requests.map((r, i) => (
              <li key={i}>
                <code>{r.kind}</code> → <code>{r.outcome}</code>{' '}
                <span className="quiet">{r.account_name} · {fmt.ago(r.created_at)}</span>
              </li>
            ))}
          </ol>
        </details>
      )}
    </section>
  );
}

/* ── OAuth pages: the sandbox consent screen, and the callback ───────────── */

export function readPublishingAuth(): { kind: 'sandbox' | 'callback'; params: URLSearchParams } | null {
  const match = window.location.hash.match(/^#\/publishing\/(sandbox|callback)\?(.*)$/);
  return match ? { kind: match[1] as 'sandbox' | 'callback', params: new URLSearchParams(match[2]) } : null;
}

export function PublishingAuth({
  kind, params, onDone,
}: { kind: 'sandbox' | 'callback'; params: URLSearchParams; onDone: () => void }) {
  const toast = useToast();
  const state = params.get('state') ?? '';
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const finish = useCallback(async (body: { state: string; code?: string; error?: string }) => {
    setBusy(true);
    setError('');
    try {
      const conn = await api<Connection>('POST', '/publishing/oauth/callback', body);
      toast(`${conn.account_name} connected`);
      window.history.replaceState(null, '', '#/publishing');
      onDone();
    } catch (err) {
      setError(message(err, 'Could not finish connecting.'));
      setBusy(false);
    }
  }, [onDone, toast]);

  // A real platform redirects back here with ?state&code (or ?error). The state is single
  // use, so redeem it once even when React runs effects twice (StrictMode in development).
  const redeemed = useRef(false);
  useEffect(() => {
    if (kind !== 'callback' || redeemed.current) return;
    redeemed.current = true;
    void finish({ state, code: params.get('code') ?? undefined, error: params.get('error') ?? undefined });
  }, [kind, state, params, finish]);

  async function decide(allow: boolean) {
    setBusy(true);
    try {
      const { code } = await api<{ code: string }>('POST', '/publishing/sandbox/authorize', {
        state, challenge: params.get('challenge') ?? '', allow,
      });
      await finish(allow ? { state, code } : { state, error: 'access_denied' });
    } catch (err) {
      setError(message(err, 'Could not reach the sandbox.'));
      setBusy(false);
    }
  }

  const leave = () => { window.history.replaceState(null, '', '#/publishing'); onDone(); };

  return (
    <main className="login">
      <div className="login-card">
        {kind === 'sandbox' ? (
          <>
            <Badge tone="neutral">Sandbox platform</Badge>
            <h1>Allow MediaFlow to post?</h1>
            <p className="login-lede">
              MediaFlow is asking to publish posts to a sandbox account. This screen stands in
              for a platform's own consent page.
            </p>
            {error && <ErrorNote message={error} />}
            <Button variant="primary" busy={busy} onClick={() => void decide(true)}>Allow</Button>
            <Button disabled={busy} onClick={() => void decide(false)}>Deny</Button>
          </>
        ) : (
          <>
            <h1>Connecting…</h1>
            {busy && <div className="center"><Spinner /></div>}
            {error && <ErrorNote message={error} />}
          </>
        )}
        {error && <Button onClick={leave}>Back to Publishing</Button>}
      </div>
    </main>
  );
}

/* ── On a calendar entry ─────────────────────────────────────────────────── */

export function PostPublishing({ postId, active }: { postId: string; active: boolean }) {
  const toast = useToast();
  const [items, setItems] = useState<Publication[] | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [choice, setChoice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');

  const load = useCallback(async () => {
    try {
      const [pubs, conns] = await Promise.all([
        api<Publication[]>('GET', `/scheduled-posts/${postId}/publications`),
        api<Connection[]>('GET', '/publishing/connections'),
      ]);
      setItems(Array.isArray(pubs) ? pubs : []);
      setConnections(Array.isArray(conns) ? conns.filter((c) => c.status === 'active') : []);
    } catch {
      setItems([]); // publishing switched off or unavailable: the calendar still works
    }
  }, [postId]);
  useEffect(() => { void load(); }, [load]);
  useLive(['publishing', 'calendar'], load);

  // Keep following deliveries that are on their way.
  useEffect(() => {
    if (!items?.some((p) => PUBLICATION_STATE[p.status].live)) return;
    const t = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(t);
  }, [items, load]);

  async function act(name: string, work: () => Promise<Publication>, done: string) {
    setBusy(name);
    setError('');
    try {
      await work();
      toast(done);
      await load();
    } catch (err) {
      setError(message(err, 'Something went wrong.'));
    } finally {
      setBusy('');
    }
  }

  if (items === null) return null;
  const attached = new Set(items.filter((p) => p.status !== 'cancelled').map((p) => p.connection_id));
  const available = connections.filter((c) => !attached.has(c.id));
  if (!items.length && !available.length) return null;
  const selected = choice || available[0]?.id || '';

  return (
    <section className="panel publishing" aria-labelledby={`pub-${postId}`}>
      <h3 id={`pub-${postId}`}>Publishing</h3>
      {items.length > 0 && (
        <ul className="rows" aria-label="Deliveries">
          {items.map((p) => {
            const s = PUBLICATION_STATE[p.status];
            return (
              <li key={p.id} className="row">
                <span className="row-main">
                  <span className="row-title">{p.account_name} · {channelLabel(p.channel)}</span>
                  <span className="row-sub">
                    {p.status === 'published' && p.published_at && `Published ${fmt.ago(p.published_at)}`}
                    {p.status === 'retrying' && p.next_attempt_at && `Attempt ${p.attempts} failed · next try ${fmt.ago(p.next_attempt_at)}`}
                    {p.last_error && p.status !== 'retrying' && p.last_error}
                    {p.status === 'retrying' && p.last_error && ` · ${p.last_error}`}
                  </span>
                </span>
                <span className="row-side">
                  <Badge tone={s.tone} live={s.live}>{s.label}</Badge>
                  {p.external_url && p.status === 'published' && (
                    <a className="link" href={p.sandbox ? '#/publishing' : p.external_url}>View</a>
                  )}
                  {(p.status === 'failed' || p.status === 'unknown') && (
                    <Button busy={busy === p.id} onClick={() => void act(p.id,
                      () => api<Publication>('POST', `/publications/${p.id}/retry`), 'Sending again')}>
                      {p.status === 'unknown' ? 'I checked: send again' : 'Retry'}
                    </Button>
                  )}
                  {(p.status === 'queued' || p.status === 'retrying') && (
                    <ConfirmButton label="Cancel" confirm="Cancel?" busy={busy === p.id}
                      onConfirm={() => void act(p.id,
                        () => api<Publication>('DELETE', `/publications/${p.id}`), 'Publication cancelled')} />
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {active && available.length > 0 && (
        <div className="form-row">
          <Field label="Also publish to">
            <select value={selected} onChange={(e) => setChoice(e.target.value)}>
              {available.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.account_name} · {channelLabel(c.channel)}{c.sandbox ? ' (sandbox)' : ''}
                </option>
              ))}
            </select>
          </Field>
          <Button variant="primary" busy={busy === 'add'} disabled={!selected}
            onClick={() => void act('add', () => api<Publication>('POST',
              `/scheduled-posts/${postId}/publications`, { connection_id: selected }),
            'It will publish at the scheduled time')}>
            Publish at this time
          </Button>
        </div>
      )}
      {error && <ErrorNote message={error} />}
      <p className="footnote">Sent automatically at the scheduled time, if the draft is still approved.</p>
    </section>
  );
}
