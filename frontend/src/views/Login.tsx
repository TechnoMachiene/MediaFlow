import { useState, type FormEvent } from 'react';
import { ApiError, api, auth } from '../api';
import type { User } from '../types';
import { Button, ErrorNote, Field } from '../ui';

export function Login({ notice, onSignedIn }: { notice: string; onSignedIn: (user: User) => void }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!email.trim() || !password) return;
    setBusy(true);
    setError('');
    try {
      // Every account password is 12+ characters, so a shorter one can only be wrong.
      if (password.length < 12) throw new ApiError(401, 'Incorrect email or password');
      const { access_token } = await api<{ access_token: string }>('POST', '/auth/login', {
        email: email.trim(), password,
      });
      auth.set(access_token);
      onSignedIn(await api<User>('GET', '/auth/me'));
    } catch (err) {
      auth.set(null);
      setError(
        !(err instanceof ApiError) ? 'Could not sign in.'
          : err.status === 422 ? 'Enter the email address and password for your account.'
          : err.message,
      );
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form className="login-card" onSubmit={submit} noValidate>
        <div className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 32 32"><path d="M9 22V10l7 8 7-8v12" /></svg>
        </div>
        <h1>Sign in to MediaFlow</h1>
        <p className="login-lede">Turn photos and videos into ready-to-review social drafts.</p>
        {notice && !error && <p className="login-notice" role="status">{notice}</p>}
        <Field label="Email">
          <input
            type="email" name="email" autoComplete="username" autoFocus required
            value={email} onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="Password">
          <input
            type="password" name="password" autoComplete="current-password" required
            value={password} onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        {error && <ErrorNote message={error} />}
        <Button variant="primary" type="submit" busy={busy} disabled={!email.trim() || !password}>
          Sign in
        </Button>
        <p className="footnote">Accounts are created by your manager or admin.</p>
      </form>
    </main>
  );
}
