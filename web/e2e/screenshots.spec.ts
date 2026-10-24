import { test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { openApp, seedSession, seedTheme, THEMES, VIEWPORTS, WIDTHS } from './helpers';

const OUT = resolve(import.meta.dirname, '../../docs/design/app-screenshots');
mkdirSync(OUT, { recursive: true });

const PAGES: [name: string, path: string][] = [
  ['dashboard', '/dashboard'],
  ['controle', '/controle'],
  ['kit', '/_kit'],
  ['login', '/login'],
];

for (const theme of THEMES) {
  for (const width of WIDTHS) {
    for (const [name, path] of PAGES) {
      test(`captura ${name} ${theme} ${width}`, async ({ page }) => {
        await page.setViewportSize(VIEWPORTS[width]);
        await seedTheme(page, theme);
        if (name !== 'login') await seedSession(page);
        await openApp(page, path);
        await page.waitForTimeout(400);
        await page.screenshot({
          path: `${OUT}/${name}-${theme}-${width}.png`,
          fullPage: name !== 'login',
        });
      });
    }
  }
}
