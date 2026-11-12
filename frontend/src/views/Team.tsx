import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { ApiError, api } from '../api';
import { fmt, isAdmin, roleLabel } from '../lib';
import { Sheet } from '../Sheet';
import { useSession } from '../session';
import { useToast } from '../toast';
import type { Page, Role, User } from '../types';
import {
  Badge, Button, ConfirmButton, Empty, ErrorNote, Field, Icon, PageHeader, Spinner, useLast,
} from '../ui';

type Editing = { mode: 'create' } | { mode: 'edit'; user: User };
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function Team() {
  const { user: me } = useSession();
  const admin = isAdmin(me.role);
  const [users, setUsers] = useState<User[] | null>(null);
  const [error, setError] = useState('');
  const [more, setMore] = useState(false);
  const [editing, setEditing] = useState<Editing | null>(null);

  const load = useCallback(async (count = 100) => {
    if (!admin) return;
    try {
      const all: User[] = [];
      let full = true;
      while (full && all.length < count) {
        const page = await api<Page<User>>('GET', `/users?limit=100&offset=${all.length}`);
        all.push(...page.items);
        full = page.items.length === 100;
      }
      setUsers(all);
      setMore(full);
      setError('');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load the team.');
    }
  }, [admin]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="page">
      <PageHeader title="Team" subtitle="People who can sign in to MediaFlow">
        <Button variant="primary" onClick={() => setEditing({ mode: 'create' })}>
          <Icon name="plus" /> Add person
        </Button>
      </PageHeader>
      <div className="page-body narrow">
        {error && <ErrorNote message={error} onRetry={() => load()} />}
        {!admin && (
          <Empty icon="team" title="Add people to your team">
            As a manager you can create member and manager accounts. Only admins can see and edit
            the full list.
          </Empty>
        )}
        {admin && users === null && !error && <div className="center"><Spinner /></div>}
        {admin && users && (
          <ul className="rows">
            {users.map((u) => (
              <li key={u.id}>
                <button type="button" className="row" onClick={() => setEditing({ mode: 'edit', user: u })}>
                  <span className="avatar" aria-hidden="true">{u.email[0].toUpperCase()}</span>
                  <span className="row-main">
                    <span className="row-title">
                      {u.email}{u.id === me.id && <span className="you"> (you)</span>}
                    </span>
                    <span className="row-sub">Added {fmt.date(u.created_at)}</span>
                  </span>
                  <span className="row-side">
                    <Badge tone={u.role === 'user' ? 'neutral' : 'blue'}>{roleLabel[u.role]}</Badge>
                    <Icon name="right" />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {admin && more && (
          <div className="more"><Button onClick={() => load((users?.length ?? 0) + 100)}>Show more</Button></div>
        )}
      </div>
      <UserSheet editing={editing} onClose={() => setEditing(null)} onSaved={() => load(users?.length || 100)} />
    </div>
  );
}

function UserSheet({
  editing: current, onClose, onSaved,
}: { editing: Editing | null; onClose: () => void; onSaved: () => void }) {
  const editing = useLast(current);
  const title = editing?.mode === 'edit' ? editing.user.email : 'Add person';
  return (
    <Sheet open={current !== null} onClose={onClose} title={title}>
      {editing && (
        <UserForm
          key={editing.mode === 'edit' ? editing.user.id : 'new'}
          editing={editing} onClose={onClose} onSaved={onSaved}
        />
      )}
    </Sheet>
  );
}

function UserForm({
  editing, onClose, onSaved,
}: { editing: Editing; onClose: () => void; onSaved: () => void }) {
  const { user: me } = useSession();
  const toast = useToast();
  const target = editing.mode === 'edit' ? editing.user : null;
  const locked = target?.role === 'super_admin';
  const [email, setEmail] = useState(target?.email ?? '');
  const [role, setRole] = useState<Role>(target?.role ?? 'user');
  const [password, setPassword] = useState('');
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  const roles: Role[] = isAdmin(me.role) ? ['user', 'manager', 'admin'] : ['user', 'manager'];
  const emailError = !EMAIL.test(email.trim()) ? 'Enter a valid email address.' : '';
  const passwordNeeded = !target || password.length > 0;
  const passwordError =
    passwordNeeded && password.length < 12 ? 'Use at least 12 characters.' : '';
  const touch = (name: string) => setTouched((t) => ({ ...t, [name]: true }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setTouched({ email: true, password: true });
    if (emailError || passwordError) return;
    setBusy('save');
    setError('');
    try {
      if (target) {
        const change: Record<string, string> = {};
        if (email.trim().toLowerCase() !== target.email) change.email = email.trim();
        if (role !== target.role) change.role = role;
        if (password) change.password = password;
        if (Object.keys(change).length) await api<User>('PATCH', `/users/${target.id}`, change);
        toast('Changes saved');
      } else {
        await api<User>('POST', '/users', { email: email.trim(), password, role });
        toast(`${email.trim()} can now sign in`);
      }
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong.');
    } finally {
      setBusy('');
    }
  }

  async function remove() {
    if (!target) return;
    setBusy('delete');
    setError('');
    try {
      await api<void>('DELETE', `/users/${target.id}`);
      toast(`${target.email} removed`);
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong.');
    } finally {
      setBusy('');
    }
  }

  if (locked) {
    return (
      <div className="callout">
        <Icon name="lock" />
        <div>
          <strong>This is the protected owner account.</strong>
          <p>It can't be edited or removed here. Reset it from the server with the admin command.</p>
        </div>
      </div>
    );
  }

  return (
    <form className="form" onSubmit={submit} noValidate>
      <Field label="Email" error={touched.email ? emailError : ''}>
        <input
          type="email" autoComplete="off" value={email} required
          onChange={(e) => setEmail(e.target.value)} onBlur={() => touch('email')}
        />
      </Field>
      <Field label="Role" hint={ROLE_HINT[role]}>
        <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          {roles.map((r) => <option key={r} value={r}>{roleLabel[r]}</option>)}
        </select>
      </Field>
      <Field
        label={target ? 'New password' : 'Password'}
        error={touched.password ? passwordError : ''}
        hint={target
          ? 'Leave empty to keep the current password. Changing it signs this person out everywhere.'
          : 'At least 12 characters. Share it with them securely.'}
      >
        <input
          type="password" autoComplete="new-password" value={password} maxLength={128}
          onChange={(e) => setPassword(e.target.value)} onBlur={() => touch('password')}
        />
      </Field>
      {error && <ErrorNote message={error} />}
      <div className="form-actions">
        {target && target.id !== me.id && (
          <ConfirmButton
            label="Remove person" confirm="Remove and delete their jobs"
            busy={busy === 'delete'} onConfirm={remove}
          />
        )}
        <span className="spacer" />
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="primary" type="submit" busy={busy === 'save'}>
          {target ? 'Save' : 'Add person'}
        </Button>
      </div>
    </form>
  );
}

const ROLE_HINT: Record<Role, string> = {
  user: 'Uploads media and schedules their own approved drafts.',
  manager: 'Also reviews drafts and adds members and managers.',
  admin: 'Also manages the team and sees all jobs and queue activity.',
  super_admin: '',
};
