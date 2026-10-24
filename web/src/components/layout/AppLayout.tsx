import { useEffect } from 'react';
import { Outlet, useNavigate } from 'react-router-dom';
import { useBotStatus } from '@/api/queries';
import { useAuth } from '@/auth/AuthContext';
import { useConnection } from '@/realtime/ConnectionProvider';
import { BottomNav } from './BottomNav';
import { StopAllBridge } from './StopAllBridge';
import { ConnectionBanner } from './ConnectionBanner';
import { SideNav } from './SideNav';
import { TopBar } from './TopBar';
import { useNavCollapsed } from './useNavCollapsed';

export function AppLayout() {
  const { data } = useBotStatus();
  const { disconnected, lastMessageAt } = useConnection();
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [collapsed, toggleCollapsed] = useNavCollapsed();
  const mode = data?.mode;

  useEffect(() => {
    if (mode) document.body.dataset.modo = mode;
    else delete document.body.dataset.modo;
    return () => {
      delete document.body.dataset.modo;
    };
  }, [mode]);

  const handleLogout = () => {
    void logout().finally(() => navigate('/login', { replace: true }));
  };
  const alertCount = data
    ? data.pause_reasons.length +
      (data.stops_blind ? 1 : 0) +
      data.limits.filter((l) => l.level === 'bad').length
    : 0;

  return (
    <StopAllBridge>
      <a
        href="#conteudo"
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[100] focus:rounded-control focus:border focus:border-border-strong focus:bg-surface focus:px-4 focus:py-2.5 focus:font-semibold"
      >
        Pular para o conteúdo
      </a>
      <div className="flex min-h-dvh">
        <SideNav
          collapsed={collapsed}
          onToggle={toggleCollapsed}
          alertCount={alertCount}
          onLogout={handleLogout}
        />
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="sticky top-0 z-50">
            <TopBar />
            {disconnected ? <ConnectionBanner lastDataAt={lastMessageAt} /> : null}
          </div>
          <main
            id="conteudo"
            tabIndex={-1}
            className="flex w-full max-w-[1280px] min-w-0 flex-col gap-4 px-4 pb-24 pt-4 outline-none sm:px-5 sm:pb-12 sm:pt-5 lg:gap-5 lg:px-7 lg:pt-6"
          >
            <Outlet />
          </main>
        </div>
      </div>
      <BottomNav alertCount={alertCount} onLogout={handleLogout} />
    </StopAllBridge>
  );
}
