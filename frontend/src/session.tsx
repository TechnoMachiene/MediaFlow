import { createContext, useContext } from 'react';
import type { Me } from './types';

export interface Session {
  user: Me;
  signOut: () => void;
  switchWorkspace: (id: string) => void;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error('useSession outside of a signed-in view');
  return session;
}
