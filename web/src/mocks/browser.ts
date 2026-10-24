import { setupWorker } from 'msw/browser';
import type { BotStatus } from '@/api/schemas';
import { fakeSocketFactory, mockControls } from './fakeSocket';
import { handlers } from './handlers';
import { patchMockStatus, resetMockStatus } from './mockState';

declare global {
  interface Window {
    __ciMock?: typeof mockControls & {
      setStatus: (patch: Partial<BotStatus>) => void;
      reset: () => void;
    };
  }
}

export async function startMocks() {
  await setupWorker(...handlers).start({ onUnhandledFrame: 'bypass', quiet: true });
  window.__ciMock = {
    ...mockControls,
    setStatus: (patch) => {
      patchMockStatus(patch);
      mockControls.push();
    },
    reset: () => {
      resetMockStatus();
      mockControls.push();
    },
  };
  return { socketFactory: fakeSocketFactory };
}
