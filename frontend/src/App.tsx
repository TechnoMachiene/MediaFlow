import { MotionConfig } from 'motion/react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, auth, clearMedia } from './api';
import { isAdmin, isReviewer, roleLabel } from './lib';
import { SessionContext } from './session';
import { Sheet } from './Sheet';
import { ToastProvider } from './toast';
import type { User } from './types';
import { Button, Icon, Spinner, type IconName } from './ui';
import { uploads } from './uploads';
import { Activity } from './views/Activity';
import { Calendar } from './views/Calendar';
import { Library } from './views/Library';
import { Login } from './views/Login';
import { Review } from './views/Review';
import { Team } from './views/Team';

type Route = 'library' | 'review' | 'calendar' | 'team' | 'activity';
const NAV: { id: Route; label: string; icon: IconName; allow: (u: User) => boolean; view: () => React.JSX.Element }[] = [
  { id: 'library', label: 'Library', icon: 'library', allow: () => true, view: Library },
  { id: 'review', label: 'Review', icon: 'review', allow: (u) => isReviewer(u.role), view: Review },
  { id: 'calendar', label: 'Calendar', icon: 'calendar', allow: () => true, view: Calendar },
  { id: 'team', label: 'Team', icon: 'team', allow: (u) => isReviewer(u.role), view: Team },
  { id: 'activity', label: 'Activity', icon: 'activity', allow: (u) => isAdmin(u.role), view: Activity },
];

const readRoute = () => window.location.hash.replace(/^#\/?/, '');

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(auth.token !== null);
  const [notice, setNotice] = useState('');

  useEffect(() => {
    auth.onUnauthorized(() => {
      clearMedia();
      setUser(null);
      setNotice('Your session ended. Sign in again to continue.');
    });
    if (auth.token) {
      api<User>('GET', '/auth/me')
        .then(setUser, () => {})
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

  const session = useMemo(() => (user ? { user, signOut } : null), [user, signOut]);

  return (
    <MotionConfig reducedMotion="user">
      <ToastProvider>
        {booting ? (
          <div className="boot"><Spinner /></div>
        ) : session ? (
          <SessionContext.Provider value={session}>
            <Shell user={session.user} signOut={signOut} />
          </SessionContext.Provider>
        ) : (
          <Login notice={notice} onSignedIn={(u) => { setNotice(''); setUser(u); }} />
        )}
      </ToastProvider>
    </MotionConfig>
  );
}

function Shell({ user, signOut }: { user: User; signOut: () => void }) {
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
          <Button onClick={signOut}>Sign out</Button>
        </div>
      </Sheet>
    </div>
  );
}
