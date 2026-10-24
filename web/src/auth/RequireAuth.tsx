import type { ReactNode } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import type { Role } from '@/api/schemas';
import { Skeleton } from '@/components/ui/skeleton';
import { useAuth } from './AuthContext';

export function FullPageLoading() {
  return (
    <div
      role="status"
      aria-live="polite"
      className="mx-auto flex min-h-dvh max-w-[480px] flex-col justify-center gap-4 p-6"
    >
      <span className="sr-only">Carregando…</span>
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-28" />
      <Skeleton className="h-28" />
    </div>
  );
}

export function RequireAuth({ role, children }: { role?: Role; children?: ReactNode }) {
  const { status, session } = useAuth();
  const location = useLocation();

  if (status === 'loading') return <FullPageLoading />;
  if (status === 'anonymous') {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  if (role && session?.user.role !== role) return <Navigate to="/dashboard" replace />;
  return children ?? <Outlet />;
}
