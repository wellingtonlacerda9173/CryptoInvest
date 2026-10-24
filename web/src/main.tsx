import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { createQueryClient } from './api/queries';
import { httpAuthBackend } from './auth/httpAuthBackend';
import { Providers } from './Providers';
import type { SocketFactory } from './realtime/useReconnectingSocket';
import './styles/index.css';

async function bootstrap() {
  let socketFactory: SocketFactory | undefined;
  // Dados ficticios (MSW) so com VITE_MOCK=1; no build normal este ramo e removido.
  if (import.meta.env.VITE_MOCK === '1') {
    const { startMocks } = await import('./mocks/browser');
    ({ socketFactory } = await startMocks());
  }

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <Providers queryClient={createQueryClient()} backend={httpAuthBackend}>
        <App socketFactory={socketFactory} />
      </Providers>
    </StrictMode>,
  );
}

void bootstrap();
