import { defineConfig } from '@playwright/test';

const port = 5199;

export default defineConfig({
  testDir: 'e2e',
  fullyParallel: true,
  retries: 0,
  timeout: 60_000,
  workers: 2,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    // Usa o Edge/Chrome instalado: evita baixar navegadores. Em CI: `npx playwright install chromium` e remova `channel`.
    channel: process.env.PW_CHANNEL ?? 'msedge',
    locale: 'pt-BR',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `npm run dev:mock -- --port ${port} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
