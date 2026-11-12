import { createContext, useContext } from 'react';
import type { User } from './types';

export interface Session {
  user: User;
  signOut: () => void;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error('useSession outside of a signed-in view');
  return session;
}
