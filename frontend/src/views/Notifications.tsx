import { useCallback, useEffect, useState } from 'react';
import { ApiError, api } from '../api';
import { fmt } from '../lib';
import type { AppNotification, NotificationList } from '../types';
import { Button, Empty, ErrorNote, Spinner } from '../ui';

interface Preferences { email: boolean; email_available: boolean }

const changed = () => window.dispatchEvent(new Event('mediaflow:notifications-changed'));

/** The notifications sheet: newest first; opening one marks it read and goes to its screen. */
export function Notifications({ onClose }: { onClose: () => void }) {
  const [list, setList] = useState<NotificationList | null>(null);
  const [prefs, setPrefs] = useState<Preferences | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(() => {
    api<NotificationList>('GET', '/notifications?limit=50').then(setList, (err) =>
      setError(err instanceof ApiError ? err.message : 'Could not load notifications.'));
  }, []);
  useEffect(() => {
    load();
    api<Preferences>('GET', '/notifications/preferences').then(setPrefs, () => {});
  }, [load]);

  async function read(body: { ids?: string[]; all?: boolean }) {
    await api<void>('POST', '/notifications/read', body).catch(() => {});
    changed();
    load();
  }

  function open(n: AppNotification) {
    if (!n.read) void read({ ids: [n.id] });
    // Straight to the item: its screen, with the draft open on top (App.tsx reads ?job=).
    if (n.route) window.location.hash = n.job_id ? `#/${n.route}?job=${n.job_id}` : `#/${n.route}`;
    onClose();
  }

  async function toggleEmail(email: boolean) {
    setPrefs(await api<Preferences>('PUT', '/notifications/preferences', { email }));
  }

  if (error) return <ErrorNote message={error} onRetry={load} />;
  if (!list) return <Spinner />;
  return (
    <div className="notifications">
      <div className="form-actions">
        {prefs?.email_available ? (
          <label className="check">
            <input type="checkbox" checked={prefs.email}
              onChange={(e) => void toggleEmail(e.target.checked)} />
            Email me as well
          </label>
        ) : <span className="quiet">Shown here only: email isn't set up on this server.</span>}
        <span className="spacer" />
        {list.unread > 0 && <Button onClick={() => void read({ all: true })}>Mark all read</Button>}
      </div>
      {list.items.length === 0 ? (
        <Empty icon="bell" title="Nothing yet">
          You'll hear here when a draft needs review, a decision is made, or a post goes live.
        </Empty>
      ) : (
        <ul className="rows" aria-label="Notifications">
          {list.items.map((n) => (
            <li key={n.id} className={`row notification ${n.read ? '' : 'unread'}`}>
              <button type="button" className="row-main" onClick={() => open(n)}>
                <span className="row-title" dir="auto">{n.title}</span>
                <span className="row-sub" dir="auto">{n.body}</span>
                <span className="row-sub">{fmt.ago(n.created_at)}</span>
              </button>
              {!n.read && <span className="unread-dot" aria-label="Unread" />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
