import { expect, test } from '@playwright/test';
import { axeScan, openApp, seedSession, seedTheme, THEMES, VIEWPORTS, WIDTHS } from './helpers';

const ROUTES = [
  '/login',
  '/onboarding',
  '/dashboard',
  '/controle',
  '/mercado',
  '/decisoes',
  '/operacoes',
  '/desempenho',
  '/backtest',
  '/configuracoes',
  '/_kit',
];

for (const theme of THEMES) {
  for (const width of WIDTHS) {
    test.describe(`axe ${theme} ${width}`, () => {
      for (const route of ROUTES) {
        test(route, async ({ page }) => {
          await page.setViewportSize(VIEWPORTS[width]);
          await seedTheme(page, theme);
          if (route !== '/login') await seedSession(page);
          await openApp(page, route);
          const violations = await axeScan(page);
          expect(violations, JSON.stringify(violations, null, 2)).toEqual([]);
        });
      }
    });
  }
}

test('dialogo Parar tudo e menu Mais sem violacoes', async ({ page }) => {
  await page.setViewportSize(VIEWPORTS[390]);
  await seedSession(page);
  await openApp(page, '/dashboard');
  await page.getByRole('button', { name: 'Parar tudo' }).click();
  await expect(page.getByRole('alertdialog')).toBeVisible();
  expect(await axeScan(page)).toEqual([]);
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Mais' }).click();
  await expect(page.getByRole('dialog', { name: 'Mais opções' })).toBeVisible();
  expect(await axeScan(page)).toEqual([]);
});

test('skip link e foco visivel por teclado', async ({ page }) => {
  await seedSession(page);
  await openApp(page, '/dashboard');
  await page.keyboard.press('Tab');
  const skip = page.getByRole('link', { name: 'Pular para o conteúdo' });
  await expect(skip).toBeFocused();
  await expect(skip).toBeInViewport();
  const outline = await skip.evaluate((el) => getComputedStyle(el).outlineWidth);
  expect(outline).toBe('3px');
  await page.keyboard.press('Enter');
  await expect(page.locator('#conteudo')).toBeFocused();
});
