import { MotionConfig } from 'motion/react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, api, auth, clearMedia } from './api';
import { isAdmin, isReviewer, roleLabel } from './lib';
import { SessionContext, useSession } from './session';
import { Sheet } from './Sheet';
import { ToastProvider } from './toast';
import type { Me, Meta, User } from './types';
import { Button, ConfirmButton, ErrorNote, Icon, Spinner, type IconName } from './ui';
import { uploads } from './uploads';
import { Activity } from './views/Activity';
import { Audit } from './views/Audit';
import { Calendar } from './views/Calendar';
import { AcceptInvite, readInviteToken } from './views/Invite';
import { Library } from './views/Library';
import { Publishing, PublishingAuth, readPublishingAuth } from './views/Publishing';
import { Login } from './views/Login';
import { Review } from './views/Review';
import { Team } from './views/Team';

type Route = 'library' | 'review' | 'calendar' | 'publishing' | 'team' | 'activity' | 'audit';
const NAV: { id: Route; label: string; icon: IconName; allow: (u: User) => boolean; view: () => React.JSX.Element }[] = [
  { id: 'library', label: 'Library', icon: 'library', allow: () => true, view: Library },
  { id: 'review', label: 'Review', icon: 'review', allow: (u) => isReviewer(u.role), view: Review },
  { id: 'calendar', label: 'Calendar', icon: 'calendar', allow: () => true, view: Calendar },
  { id: 'publishing', label: 'Publishing', icon: 'upload', allow: (u) => isAdmin(u.role), view: Publishing },
  { id: 'team', label: 'Team', icon: 'team', allow: (u) => isReviewer(u.role), view: Team },
  { id: 'activity', label: 'Activity', icon: 'activity', allow: (u) => isAdmin(u.role), view: Activity },
  { id: 'audit', label: 'Audit', icon: 'lock', allow: (u) => isAdmin(u.role), view: Audit },
];

const readRoute = () => window.location.hash.replace(/^#\/?/, '');
const NO_META: Meta = { demo_mode: false, allow_signup: false, demo_password: null, demo_accounts: [] };

export default function App() {
  const [user, setUser] = useState<Me | null>(null);
  const [meta, setMeta] = useState<Meta>(NO_META);
  const [booting, setBooting] = useState(auth.token !== null);
  const [notice, setNotice] = useState('');
  const [invite, setInvite] = useState(readInviteToken);
  const [oauth, setOauth] = useState(readPublishingAuth);

  useEffect(() => {
    const onHash = () => { setInvite(readInviteToken()); setOauth(readPublishingAuth()); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    api<Meta>('GET', '/meta').then(setMeta, () => {});
    auth.onUnauthorized(() => {
      clearMedia();
      setUser(null);
      setNotice('Your session ended. Sign in again to continue.');
    });
    if (auth.token) {
      api<Me>('GET', '/auth/me')
        .then(setUser, () => auth.setWorkspace(null))
        .finally(() => setBooting(false));
    }
  }, []);

  const signOut = useCallback(() => {
    auth.set(null);
    clearMedia();
    uploads.reset();
    setNotice('');
    setUser(null);
  }, []);

  const switchWorkspace = useCallback((id: string) => {
    const previous = auth.workspace;
    auth.setWorkspace(id);
    api<Me>('GET', '/auth/me').then(
      (me) => {
        clearMedia();
        uploads.reset();
        setUser(me);
      },
      () => auth.setWorkspace(previous),
    );
  }, []);

  const session = useMemo(
    () => (user ? { user, signOut, switchWorkspace } : null),
    [user, signOut, switchWorkspace],
  );

  return (
    <MotionConfig reducedMotion="user">
      <ToastProvider>
        {meta.demo_mode && (
          <p className="demo-banner" role="note">
            <strong>Demo mode.</strong> Drafts are written by an offline stand-in, not Gemini.
          </p>
        )}
        {booting ? (
          <div className="boot"><Spinner /></div>
        ) : invite ? (
          <AcceptInvite
            token={invite} user={user} onSignOut={signOut}
            onJoined={(u) => { setInvite(null); setNotice(''); setUser(u); }}
            onLeave={() => { window.history.replaceState(null, '', '#/'); setInvite(null); }}
          />
        ) : session && oauth ? (
          <SessionContext.Provider value={session}>
            <PublishingAuth kind={oauth.kind} params={oauth.params} onDone={() => setOauth(null)} />
          </SessionContext.Provider>
        ) : session ? (
          <SessionContext.Provider value={session}>
            {/* Remount every view when the workspace changes so no tenant's data lingers. */}
            <Shell key={session.user.workspace.id} user={session.user} signOut={signOut} />
          </SessionContext.Provider>
        ) : (
          <Login
            notice={notice} meta={meta}
            onSignedIn={(u) => { setNotice(''); setUser(u); }}
          />
        )}
      </ToastProvider>
    </MotionConfig>
  );
}

export function WorkspaceSwitcher({ user, onSwitch }: { user: Me; onSwitch: (id: string) => void }) {
  if (user.workspaces.length < 2) {
    return <span className="workspace-name" title={user.workspace.name}>{user.workspace.name}</span>;
  }
  return (
    <label className="workspace-switch">
      <span className="visually-hidden">Workspace</span>
      <select value={user.workspace.id} onChange={(e) => onSwitch(e.target.value)}>
        {user.workspaces.map((w) => (
          <option key={w.id} value={w.id}>{w.name} · {roleLabel[w.role]}</option>
        ))}
      </select>
    </label>
  );
}

function Shell({ user, signOut }: { user: Me; signOut: () => void }) {
  const [hash, setHash] = useState(readRoute);
  const [account, setAccount] = useState(false);

  useEffect(() => {
    const onHash = () => setHash(readRoute());
    const onAccount = () => setAccount(true);
    window.addEventListener('hashchange', onHash);
    window.addEventListener('mediaflow:account', onAccount);
    return () => {
      window.removeEventListener('hashchange', onHash);
      window.removeEventListener('mediaflow:account', onAccount);
    };
  }, []);

  const items = NAV.filter((n) => n.allow(user));
  const current = items.find((n) => n.id === hash) ?? items[0];
  const View = current.view;

  useEffect(() => {
    document.title = `${current.label} · MediaFlow`;
    window.scrollTo(0, 0);
  }, [current]);

  const links = (className: string) =>
    items.map((n) => (
      <a
        key={n.id} href={`#/${n.id}`} className={className}
        aria-current={n.id === current.id ? 'page' : undefined}
      >
        <Icon name={n.icon} />
        <span>{n.label}</span>
      </a>
    ));

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 32 32"><path d="M9 22V10l7 8 7-8v12" /></svg>
          </span>
          MediaFlow
        </div>
        <SwitcherFromSession user={user} />
        <nav aria-label="Main">{links('nav-link')}</nav>
        <div className="sidebar-account">
          <span className="avatar" aria-hidden="true">{user.email[0].toUpperCase()}</span>
          <span className="sidebar-who">
            <span title={user.email}>{user.email}</span>
            <small>{roleLabel[user.role]}</small>
          </span>
          <button type="button" className="link" onClick={signOut}>Sign out</button>
        </div>
      </aside>

      <main className="main">
        <div key={current.id} className="view"><View /></div>
      </main>

      <nav className="tabbar" aria-label="Main">{links('tab')}</nav>

      <Sheet open={account} onClose={() => setAccount(false)} title="Account">
        <div className="account">
          <span className="avatar avatar-lg" aria-hidden="true">{user.email[0].toUpperCase()}</span>
          <strong>{user.email}</strong>
          <span className="quiet">{roleLabel[user.role]}</span>
          <SwitcherFromSession user={user} />
          <LeaveWorkspace user={user} />
          <Button onClick={signOut}>Sign out</Button>
        </div>
      </Sheet>
    </div>
  );
}

/** Only offered when you belong to another workspace too; the server enforces the same rules. */
export function LeaveWorkspace({ user }: { user: Me }) {
  const { switchWorkspace } = useSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const next = user.workspaces.find((w) => w.id !== user.workspace.id);
  if (!next) return null;

  async function leave() {
    setBusy(true);
    setError('');
    try {
      await api<void>('POST', `/workspaces/${user.workspace.id}/leave`);
      switchWorkspace(next!.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not leave the workspace.');
      setBusy(false);
    }
  }

  return (
    <>
      <ConfirmButton
        label={`Leave ${user.workspace.name}`} confirm="Leave? Your work stays with them"
        busy={busy} onConfirm={() => void leave()}
      />
      {error && <ErrorNote message={error} />}
    </>
  );
}

function SwitcherFromSession({ user }: { user: Me }) {
  const { switchWorkspace } = useSession();
  return <WorkspaceSwitcher user={user} onSwitch={switchWorkspace} />;
}
