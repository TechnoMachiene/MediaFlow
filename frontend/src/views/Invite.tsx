import { useEffect, useState, type FormEvent } from 'react';
import { ApiError, api, auth } from '../api';
import { fmt, roleLabel } from '../lib';
import type { InvitationAccepted, InvitationPreview, Me } from '../types';
import { Button, ErrorNote, Field, Spinner } from '../ui';

/** `#/invite/<token>`. The token lives in the fragment, which browsers never send to servers. */
export function readInviteToken(): string | null {
  const match = window.location.hash.match(/^#\/invite\/([A-Za-z0-9_-]{20,128})$/);
  return match ? match[1] : null;
}

const message = (err: unknown, fallback: string) =>
  err instanceof ApiError ? err.message : fallback;

/**
 * Accepting an invitation. Three cases, decided by the server's preview:
 * signed in as the invited email (one click), an existing account (sign in, then join),
 * or someone new (choose a password; the inviter never knows it).
 */
export function AcceptInvite({
  token, user, onJoined, onSignOut, onLeave,
}: {
  token: string;
  user: Me | null;
  onJoined: (me: Me) => void;
  onSignOut: () => void;
  onLeave: () => void;
}) {
  const [preview, setPreview] = useState<InvitationPreview | null>(null);
  const [loadError, setLoadError] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api<InvitationPreview>('POST', '/invitations/lookup', { token }).then(
      setPreview,
      (err) => setLoadError(message(err, 'Could not open this invitation.')),
    );
  }, [token]);

  const wrongAccount = user && preview && user.email.toLowerCase() !== preview.email;
  const newAccount = !user && preview && !preview.account_exists;
  const needsPassword = !user && preview !== null;
  const passwordError = newAccount && password.length > 0 && password.length < 12
    ? 'Use at least 12 characters.' : '';

  async function join(e: FormEvent) {
    e.preventDefault();
    if (!preview || (needsPassword && (!password || passwordError))) return;
    setBusy(true);
    setError('');
    try {
      if (!user && preview.account_exists) {
        const { access_token } = await api<{ access_token: string }>('POST', '/auth/login', {
          email: preview.email, password,
        });
        auth.set(access_token);
      }
      const joined = await api<InvitationAccepted>(
        'POST', '/invitations/accept', newAccount ? { token, password } : { token },
      );
      auth.set(joined.access_token);
      auth.setWorkspace(joined.workspace.id);
      const me = await api<Me>('GET', '/auth/me');
      window.history.replaceState(null, '', '#/library');
      onJoined(me);
    } catch (err) {
      if (!user) auth.set(null); // a sign-in made just for this attempt doesn't linger
      setError(message(err, 'Could not join the workspace.'));
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form className="login-card" onSubmit={join} noValidate>
        <div className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 32 32"><path d="M9 22V10l7 8 7-8v12" /></svg>
        </div>
        {loadError ? (
          <>
            <h1>This invitation can't be used</h1>
            <ErrorNote message={loadError} />
            <Button onClick={onLeave}>Go to MediaFlow</Button>
          </>
        ) : !preview ? (
          <div className="center"><Spinner /></div>
        ) : (
          <>
            <h1>Join {preview.workspace_name}</h1>
            <p className="login-lede">
              You're invited as {/^[AEIOU]/.test(roleLabel[preview.role]) ? 'an' : 'a'}{' '}
              {roleLabel[preview.role]} for <strong>{preview.email}</strong>.
            </p>

            {wrongAccount ? (
              <>
                <p className="login-notice" role="status">
                  You're signed in as {user?.email}. Sign out, then open the link again to accept as{' '}
                  {preview.email}.
                </p>
                <Button variant="primary" onClick={onSignOut}>Sign out</Button>
              </>
            ) : (
              <>
                {needsPassword && (
                  <Field
                    label={newAccount ? 'Choose a password' : 'Password'}
                    error={passwordError}
                    hint={newAccount ? 'At least 12 characters. Only you will know it.'
                      : 'You already have an account. Sign in to join.'}
                  >
                    <input
                      type="password" name="password" required autoFocus maxLength={128}
                      autoComplete={newAccount ? 'new-password' : 'current-password'}
                      value={password} onChange={(e) => setPassword(e.target.value)}
                    />
                  </Field>
                )}
                {error && <ErrorNote message={error} />}
                <Button
                  variant="primary" type="submit" busy={busy}
                  disabled={needsPassword && (!password || passwordError !== '')}
                >
                  {user ? 'Join workspace' : newAccount ? 'Create account and join' : 'Sign in and join'}
                </Button>
              </>
            )}
            <p className="footnote">
              This link works once and expires {fmt.date(preview.expires_at)}.{' '}
              <button type="button" className="link" onClick={onLeave}>Not now</button>
            </p>
          </>
        )}
      </form>
    </main>
  );
}
