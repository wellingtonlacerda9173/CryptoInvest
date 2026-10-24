import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { BrowserRouter } from 'react-router-dom';
import { AuthProvider } from '@/auth/AuthContext';
import { ReauthProvider } from '@/auth/ReauthContext';
import type { AuthBackend } from '@/auth/types';
import { Toaster } from '@/components/kit/Toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import { ThemeProvider } from '@/theme/ThemeProvider';

export function Providers({
  queryClient,
  backend,
  children,
}: {
  queryClient: QueryClient;
  backend: AuthBackend;
  children: ReactNode;
}) {
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <AuthProvider backend={backend}>
            <ReauthProvider>
              <BrowserRouter>{children}</BrowserRouter>
            </ReauthProvider>
            <Toaster />
          </AuthProvider>
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
