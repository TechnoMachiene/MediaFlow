import { AnimatePresence, motion } from 'motion/react';
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react';
import { spring } from './lib';
import { Icon } from './ui';

type Kind = 'success' | 'error' | 'notice';
interface Toast {
  id: number;
  kind: Kind;
  text: string;
}

const ToastContext = createContext<(text: string, kind?: Kind) => void>(() => {});
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);

  const push = useCallback((text: string, kind: Kind = 'success') => {
    const id = ++seq.current;
    setToasts((list) => [...list.slice(-2), { id, kind, text }]);
    window.setTimeout(
      () => setToasts((list) => list.filter((t) => t.id !== id)),
      kind === 'success' ? 3500 : 6000,
    );
  }, []);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        <AnimatePresence>
          {toasts.map((t) => (
            <motion.div
              key={t.id}
              layout
              className={`toast toast-${t.kind}`}
              // Materialize: scale and blur arrive together, and leave the way they came.
              initial={{ opacity: 0, y: 20, scale: 0.94, filter: 'blur(8px)' }}
              animate={{ opacity: 1, y: 0, scale: 1, filter: 'blur(0px)' }}
              exit={{ opacity: 0, y: 20, scale: 0.94, filter: 'blur(8px)' }}
              transition={spring(0.35, 1)}
            >
              <Icon name={t.kind === 'error' ? 'alert' : t.kind === 'notice' ? 'bell' : 'check'} />
              <span>{t.text}</span>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  );
}
