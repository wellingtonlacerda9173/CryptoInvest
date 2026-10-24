import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';
import { setCsrfToken } from '@/api/csrf';
import { AuthProvider } from '@/auth/AuthContext';
import { ReauthProvider } from '@/auth/ReauthContext';
import type { AuthBackend, Session } from '@/auth/types';
import { Toaster } from '@/components/kit/Toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import {
  makeAccount,
  makeDecisions,
  makeEquity,
  makeGate,
  makeOnboarding,
  makePositions,
  makeSessions,
  makeStatus,
} from '@/mocks/mockState';
import { ThemeProvider } from '@/theme/ThemeProvider';

export type Handler = (body: unknown, url: URL) => unknown;
export type Routes_ = Record<string, Handler | undefined>;

export const problem = (status: number, title: string, detail = '', type = 'about:blank') =>
  new Response(JSON.stringify({ type, title, status, detail }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });

export const FRESH_REQUIRED = () =>
  problem(
    403,
    'Confirme sua identidade',
    'Confirme sua identidade.',
    'https://cryptoinvest.local/problems/fresh-auth-required',
  );

/** Leituras padrao do painel (dados ficticios); cada teste sobrescreve o que quiser. */
export const defaultRoutes = (): Routes_ => ({
  'GET /status': () => makeStatus(),
  'GET /account/summary': () => makeAccount(),
  'GET /equity': () => makeEquity(),
  'GET /decisions': () => makeDecisions(),
  'GET /positions': () => makePositions(),
  'GET /live-gate': () => makeGate(),
  'GET /control/commands': () => ({ items: [], next_cursor: null }),
  'GET /onboarding': () => makeOnboarding(false),
  'GET /sessions': () => makeSessions(),
});

export interface Call {
  method: string;
  path: string;
  body: unknown;
  search: string;
}

/** Substitui `fetch`: roteia por "METODO /caminho" (sem query) e registra as chamadas. */
export function stubApi(overrides: Routes_ = {}) {
  const routes = { ...defaultRoutes(), ...overrides };
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const path = url.pathname.replace(/^\/api/, '');
    const method = (init?.method ?? 'GET').toUpperCase();
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined;
    calls.push({ method, path, body, search: url.search });
    const handler = routes[`${method} ${path}`];
    if (!handler) return problem(404, 'Não encontrado');
    const out = handler(body, url);
    if (out instanceof Response) return out;
    return out === undefined
      ? new Response(null, { status: 204 })
      : new Response(JSON.stringify(out), { status: 200 });
  });
  vi.stubGlobal('fetch', fetchMock);
  return {
    calls,
    fetchMock,
    find: (method: string, path: string) =>
      calls.filter((c) => c.method === method && c.path === path),
  };
}

export const session = (role: 'admin' | 'reader' = 'admin'): Session => ({
  user: { id: role === 'admin' ? '1' : '2', name: role === 'admin' ? 'ana.silva' : 'leitor', role },
  freshAuthUntil: null,
});

export const fakeBackend = (
  s: Session | null,
  overrides: Partial<AuthBackend> = {},
): AuthBackend => ({
  getSession: () => Promise.resolve(s),
  login: () => Promise.resolve({ stage: 'totp' }),
  verifySecondFactor: () => Promise.resolve(session()),
  startEnrollment: () => Promise.reject(new Error('n/a')),
  confirmEnrollment: () => Promise.reject(new Error('n/a')),
  reauth: () => Promise.resolve(session()),
  logout: () => Promise.resolve(),
  ...overrides,
});

export function renderPage(
  ui: ReactElement,
  {
    role = 'admin',
    path = '/',
    backend,
  }: { role?: 'admin' | 'reader' | null; path?: string; backend?: AuthBackend } = {},
) {
  setCsrfToken('csrf-de-teste');
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0, refetchInterval: false } },
  });
  const b = backend ?? fakeBackend(role === null ? null : session(role));
  const view = render(
    <ThemeProvider>
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <AuthProvider backend={b}>
            <ReauthProvider>
              <MemoryRouter initialEntries={[path]}>
                <Routes>
                  <Route path="*" element={ui} />
                </Routes>
              </MemoryRouter>
            </ReauthProvider>
            <Toaster />
          </AuthProvider>
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return { ...view, client };
}
