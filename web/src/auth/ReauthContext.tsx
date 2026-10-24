import { createContext, use, useCallback, useMemo, useRef, useState, type ReactNode } from 'react';
import { ApiError } from '@/api/http';
import { ConfirmDialog } from '@/components/kit/ConfirmDialog';
import { Field, Input } from '@/components/ui/input';
import { useAuth } from './AuthContext';

export class ReauthCancelled extends Error {
  constructor() {
    super('Confirmação cancelada.');
    this.name = 'ReauthCancelled';
  }
}

type RunFresh = <T>(action: () => Promise<T>) => Promise<T>;
const ReauthContext = createContext<RunFresh | null>(null);

interface Waiter {
  resolve: () => void;
  reject: (reason: Error) => void;
}

function reauthMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return 'Muitas tentativas. Aguarde um pouco e tente de novo.';
    if (error.isNetwork) return 'Sem conexão com o servidor. Verifique a internet.';
  }
  return 'Esse código não confere. Use o código atual do aplicativo.';
}

/**
 * Acoes sensiveis exigem TOTP recente. O servidor decide (403 "fresh-auth-required"): o painel abre o
 * dialogo, reautentica e repete a acao uma unica vez; nada depende do relogio do navegador.
 */
export function ReauthProvider({ children }: { children: ReactNode }) {
  const { reauth } = useAuth();
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState('');
  const waiter = useRef<Waiter | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const ask = useCallback(
    () =>
      new Promise<void>((resolve, reject) => {
        waiter.current?.reject(new ReauthCancelled());
        waiter.current = { resolve, reject };
        setCode('');
        setOpen(true);
      }),
    [],
  );

  const runFresh = useCallback<RunFresh>(
    async (action) => {
      try {
        return await action();
      } catch (error) {
        if (!(error instanceof ApiError && error.needsFreshAuth)) throw error;
        await ask();
        return action();
      }
    },
    [ask],
  );

  const close = (next: boolean) => {
    if (next) return;
    setOpen(false);
    waiter.current?.reject(new ReauthCancelled());
    waiter.current = null;
  };

  const value = useMemo(() => runFresh, [runFresh]);

  return (
    <ReauthContext value={value}>
      {children}
      <ConfirmDialog
        open={open}
        onOpenChange={close}
        title="Confirme que é você"
        description="Esta ação é sensível. Digite o código de 6 números do aplicativo autenticador."
        confirmLabel="Confirmar"
        confirmDisabled={code.length !== 6}
        initialFocusRef={inputRef}
        onConfirm={async () => {
          try {
            await reauth(code);
          } catch (error) {
            throw new Error(reauthMessage(error), { cause: error });
          }
          waiter.current?.resolve();
          waiter.current = null;
        }}
      >
        <Field label="Código do aplicativo autenticador" htmlFor="reauth-code">
          <Input
            id="reauth-code"
            ref={inputRef}
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
            className="text-center text-[20px] font-semibold tracking-[0.5em] tabular-nums"
          />
        </Field>
      </ConfirmDialog>
    </ReauthContext>
  );
}

export function useFreshAuth(): RunFresh {
  const ctx = use(ReauthContext);
  if (!ctx) throw new Error('useFreshAuth fora do ReauthProvider');
  return ctx;
}
