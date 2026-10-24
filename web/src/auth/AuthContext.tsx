import { useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, use, useCallback, useEffect, useMemo, type ReactNode } from 'react';
import { onUnauthorized } from '@/api/http';
import type { AuthBackend, EnrollmentResult, EnrollmentStart, LoginStage, Session } from './types';

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

interface AuthApi {
  status: AuthStatus;
  session: Session | null;
  login: (username: string, password: string) => Promise<{ stage: LoginStage }>;
  verifySecondFactor: (input: { code: string } | { recoveryCode: string }) => Promise<Session>;
  startEnrollment: (activationToken: string) => Promise<EnrollmentStart>;
  /** Confirma o 2FA; a sessao so e adotada em `finishEnrollment`, depois de o usuario guardar os codigos. */
  confirmEnrollment: (code: string) => Promise<EnrollmentResult>;
  finishEnrollment: (session: Session) => void;
  reauth: (code: string) => Promise<void>;
  logout: () => Promise<void>;
}

export const SESSION_KEY = ['session'] as const;
const AuthContext = createContext<AuthApi | null>(null);

export function AuthProvider({ backend, children }: { backend: AuthBackend; children: ReactNode }) {
  const queryClient = useQueryClient();
  const { data, isPending } = useQuery({
    queryKey: SESSION_KEY,
    queryFn: () => backend.getSession(),
    staleTime: Infinity,
    retry: false,
  });

  useEffect(
    () =>
      onUnauthorized(() => {
        if (queryClient.getQueryData<Session | null>(SESSION_KEY)) {
          try {
            sessionStorage.setItem('ci-session-ended', '1');
          } catch {
            // sem armazenamento: o aviso de sessao encerrada simplesmente nao aparece
          }
        }
        queryClient.setQueryData<Session | null>(SESSION_KEY, null);
      }),
    [queryClient],
  );

  const verifySecondFactor = useCallback<AuthApi['verifySecondFactor']>(
    async (input) => {
      const session = await backend.verifySecondFactor(input);
      queryClient.setQueryData(SESSION_KEY, session);
      return session;
    },
    [backend, queryClient],
  );

  const reauth = useCallback<AuthApi['reauth']>(
    async (code) => {
      queryClient.setQueryData(SESSION_KEY, await backend.reauth(code));
    },
    [backend, queryClient],
  );

  const finishEnrollment = useCallback<AuthApi['finishEnrollment']>(
    (session) => queryClient.setQueryData(SESSION_KEY, session),
    [queryClient],
  );

  const logout = useCallback(async () => {
    try {
      await backend.logout();
    } finally {
      queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== SESSION_KEY[0] });
      queryClient.setQueryData(SESSION_KEY, null);
    }
  }, [backend, queryClient]);

  const value = useMemo<AuthApi>(
    () => ({
      status: isPending ? 'loading' : data ? 'authenticated' : 'anonymous',
      session: data ?? null,
      login: (username, password) => backend.login(username, password),
      verifySecondFactor,
      startEnrollment: (token) => backend.startEnrollment(token),
      confirmEnrollment: (code) => backend.confirmEnrollment(code),
      finishEnrollment,
      reauth,
      logout,
    }),
    [backend, data, finishEnrollment, isPending, logout, reauth, verifySecondFactor],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth() {
  const ctx = use(AuthContext);
  if (!ctx) throw new Error('useAuth fora do AuthProvider');
  return ctx;
}
