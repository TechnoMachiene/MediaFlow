import { useState, type FormEvent } from 'react';
import { ApiError, api, auth } from '../api';
import { roleLabel } from '../lib';
import type { Me, Meta } from '../types';
import { Button, ErrorNote, Field } from '../ui';

type Mode = 'signin' | 'signup';

export function Login({
  notice, meta, onSignedIn,
}: { notice: string; meta: Meta; onSignedIn: (user: Me) => void }) {
  const [mode, setMode] = useState<Mode>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [workspace, setWorkspace] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const signup = mode === 'signup';

  async function signIn(address: string, secret: string, as: string) {
    setBusy(as);
    setError('');
    try {
      // Every account password is 12+ characters, so a shorter one can only be wrong.
      if (secret.length < 12) throw new ApiError(401, 'Incorrect email or password');
      auth.set(null);
      const { access_token } = signup
        ? await api<{ access_token: string }>('POST', '/auth/signup', {
          email: address, password: secret, workspace_name: workspace.trim(),
        })
        : await api<{ access_token: string }>('POST', '/auth/login', {
          email: address, password: secret,
        });
      auth.set(access_token);
      onSignedIn(await api<Me>('GET', '/auth/me'));
    } catch (err) {
      auth.set(null);
      setError(
        !(err instanceof ApiError) ? 'Could not sign in.'
          : err.status === 422 ? 'Enter the email address and password for your account.'
          : err.message,
      );
      setBusy('');
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!email.trim() || !password || (signup && !workspace.trim())) return;
    void signIn(email.trim(), password, 'form');
  }

  const ready = email.trim() && password && (!signup || workspace.trim());

  return (
    <main className="login">
      <form className="login-card" onSubmit={submit} noValidate>
        <div className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 32 32"><path d="M9 22V10l7 8 7-8v12" /></svg>
        </div>
        <h1>{signup ? 'Create your workspace' : 'Sign in to MediaFlow'}</h1>
        <p className="login-lede">Turn photos and videos into ready-to-review social drafts.</p>
        {notice && !error && <p className="login-notice" role="status">{notice}</p>}

        {meta.demo_mode && !signup && meta.demo_password && (
          <section className="demo-accounts" aria-labelledby="demo-title">
            <h2 id="demo-title">Try a demo account</h2>
            <p>Each role sees different screens. No Gemini key needed.</p>
            <div className="demo-account-list">
              {meta.demo_accounts.map((a) => (
                <Button
                  key={a.email} variant="tinted" busy={busy === a.email}
                  disabled={busy !== '' && busy !== a.email}
                  onClick={() => void signIn(a.email, meta.demo_password!, a.email)}
                >
                  Continue as {roleLabel[a.role]}
                </Button>
              ))}
            </div>
          </section>
        )}

        {signup && (
          <Field label="Workspace name" hint="Your team, brand or client. You'll be its admin.">
            <input
              name="workspace" autoComplete="organization" required maxLength={120}
              value={workspace} onChange={(e) => setWorkspace(e.target.value)}
            />
          </Field>
        )}
        <Field label="Email">
          <input
            type="email" name="email" autoComplete="username" autoFocus={!meta.demo_mode} required
            value={email} onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="Password" hint={signup ? 'At least 12 characters.' : undefined}>
          <input
            type="password" name="password" required
            autoComplete={signup ? 'new-password' : 'current-password'}
            value={password} onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        {error && <ErrorNote message={error} />}
        <Button variant="primary" type="submit" busy={busy === 'form'} disabled={!ready || busy !== ''}>
          {signup ? 'Create workspace' : 'Sign in'}
        </Button>
        {meta.allow_signup ? (
          <p className="footnote">
            {signup ? 'Already have an account? ' : 'New here? '}
            <button
              type="button" className="link"
              onClick={() => { setMode(signup ? 'signin' : 'signup'); setError(''); }}
            >
              {signup ? 'Sign in' : 'Create a workspace'}
            </button>
          </p>
        ) : (
          <p className="footnote">Accounts are created by your manager or admin.</p>
        )}
      </form>
    </main>
  );
}
