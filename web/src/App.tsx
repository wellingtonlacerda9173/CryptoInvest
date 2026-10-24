import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { RequireAuth } from '@/auth/RequireAuth';
import { AppLayout } from '@/components/layout/AppLayout';
import { ConnectionProvider } from '@/realtime/ConnectionProvider';
import type { SocketFactory } from '@/realtime/useReconnectingSocket';
import ContaPage from '@/pages/ContaPage';
import ControlePage from '@/pages/ControlePage';
import DashboardPage from '@/pages/DashboardPage';
import LoginPage from '@/pages/LoginPage';
import NotFoundPage from '@/pages/NotFoundPage';
import OnboardingPage from '@/pages/OnboardingPage';
import { Skeleton } from '@/components/ui/skeleton';

// Telas com graficos ficam em pacotes separados: o login e o dashboard nao carregam Lightweight Charts.
const MercadoPage = lazy(() => import('@/pages/MercadoPage'));
const DecisoesPage = lazy(() => import('@/pages/DecisoesPage'));
const OperacoesPage = lazy(() => import('@/pages/OperacoesPage'));
const DesempenhoPage = lazy(() => import('@/pages/DesempenhoPage'));
const BacktestPage = lazy(() => import('@/pages/BacktestPage'));
const ConfiguracoesPage = lazy(() => import('@/pages/ConfiguracoesPage'));

const lazyPage = (page: React.ReactNode) => (
  <Suspense fallback={<Skeleton className="h-64" aria-busy="true" />}>{page}</Suspense>
);

// A pagina /_kit so existe no servidor de desenvolvimento; o bundler remove este ramo no build.
const KitPage = import.meta.env.DEV ? lazy(() => import('@/pages/KitPage')) : null;

export function App({ socketFactory }: { socketFactory?: SocketFactory }) {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route path="/onboarding" element={<OnboardingPage />} />
        <Route
          element={
            <ConnectionProvider factory={socketFactory}>
              <AppLayout />
            </ConnectionProvider>
          }
        >
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/controle" element={<ControlePage />} />
          <Route path="/mercado" element={lazyPage(<MercadoPage />)} />
          <Route path="/decisoes" element={lazyPage(<DecisoesPage />)} />
          <Route path="/operacoes" element={lazyPage(<OperacoesPage />)} />
          <Route path="/desempenho" element={lazyPage(<DesempenhoPage />)} />
          <Route path="/backtest" element={lazyPage(<BacktestPage />)} />
          <Route path="/conta" element={<ContaPage />} />
          <Route path="/configuracoes" element={lazyPage(<ConfiguracoesPage />)} />
          {KitPage ? (
            <Route
              path="/_kit"
              element={
                <Suspense fallback={null}>
                  <KitPage />
                </Suspense>
              }
            />
          ) : null}
        </Route>
      </Route>
      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
