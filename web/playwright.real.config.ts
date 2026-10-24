import { defineConfig } from '@playwright/test';

const panelPort = 5198;

// E2E contra a API REAL (banco *_test descartavel, Redis real, robo de mentira): `npm run test:e2e:real`.
export default defineConfig({
  testDir: 'e2e-real',
  globalSetup: './e2e-real/global-setup.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${panelPort}`,
    channel: process.env.PW_CHANNEL ?? 'msedge',
    locale: 'pt-BR',
    trace: process.env.PW_TRACE === '1' ? 'retain-on-failure' : 'off',
  },
  webServer: {
    command: `npm run dev -- --port ${panelPort} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${panelPort}`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: { VITE_API_TARGET: 'http://127.0.0.1:8765' },
  },
});
