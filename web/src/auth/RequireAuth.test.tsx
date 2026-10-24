import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { AuthProvider } from './AuthContext';
import { RequireAuth } from './RequireAuth';
import type { AuthBackend, Session } from './types';

const backend = (session: Session | null, delayed = false): AuthBackend => ({
  getSession: () => (delayed ? new Promise(() => {}) : Promise.resolve(session)),
  login: () => Promise.resolve({ stage: 'totp' }),
  verifySecondFactor: () => Promise.reject(new Error('n/a')),
  startEnrollment: () => Promise.reject(new Error('n/a')),
  confirmEnrollment: () => Promise.reject(new Error('n/a')),
  reauth: () => Promise.reject(new Error('n/a')),
  logout: () => Promise.resolve(),
});

const admin: Session = { user: { id: '1', name: 'ana', role: 'admin' }, freshAuthUntil: null };
const reader: Session = { user: { id: '2', name: 'leo', role: 'reader' }, freshAuthUntil: null };

function Login() {
  const from = (useLocation().state as { from?: string } | null)?.from;
  return <p>login (volta para {from})</p>;
}

const renderAt = (path: string, b: AuthBackend, role?: 'admin') =>
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <AuthProvider backend={b}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route element={<RequireAuth role={role} />}>
              <Route path="/dashboard" element={<p>painel</p>} />
              <Route path="/configuracoes" element={<p>config</p>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );

describe('RequireAuth', () => {
  it('sem sessao redireciona ao login guardando a rota de origem', async () => {
    renderAt('/dashboard?x=1', backend(null));
    expect(await screen.findByText('login (volta para /dashboard?x=1)')).toBeInTheDocument();
    expect(screen.queryByText('painel')).not.toBeInTheDocument();
  });

  it('com sessao mostra a rota', async () => {
    renderAt('/dashboard', backend(admin));
    expect(await screen.findByText('painel')).toBeInTheDocument();
  });

  it('enquanto confere a sessao mostra carregamento, nunca o conteudo nem o login', () => {
    renderAt('/dashboard', backend(null, true));
    expect(screen.getByRole('status')).toHaveTextContent('Carregando');
    expect(screen.queryByText('painel')).not.toBeInTheDocument();
    expect(screen.queryByText(/login/)).not.toBeInTheDocument();
  });

  it('papel insuficiente volta ao dashboard', async () => {
    renderAt('/configuracoes', backend(reader), 'admin');
    expect(await screen.queryByText('config')).not.toBeInTheDocument();
  });

  it('administrador acessa rota restrita', async () => {
    renderAt('/configuracoes', backend(admin), 'admin');
    expect(await screen.findByText('config')).toBeInTheDocument();
  });
});
