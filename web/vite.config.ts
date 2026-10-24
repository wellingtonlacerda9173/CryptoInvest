import { rmSync } from 'node:fs';
import { fileURLToPath, URL } from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig, type Plugin } from 'vite';

const dropMockWorker = (mode: string): Plugin => ({
  name: 'drop-mock-worker',
  apply: 'build',
  closeBundle() {
    if (mode !== 'mock') rmSync('dist/mockServiceWorker.js', { force: true });
  },
});

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss(), dropMockWorker(mode)],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: false,
        ws: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
  build: { sourcemap: false, target: 'es2023' },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
    restoreMocks: true,
  },
}));
