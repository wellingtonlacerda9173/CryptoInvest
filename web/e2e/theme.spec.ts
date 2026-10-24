import { expect, test } from '@playwright/test';
import { openApp, seedSession } from './helpers';

test.describe('tema', () => {
  test.beforeEach(async ({ page }) => seedSession(page));

  test('padrao escuro, alterna em um clique e persiste ao recarregar', async ({ page }) => {
    await openApp(page, '/dashboard');
    const html = page.locator('html');
    await expect(html).toHaveClass(/dark/);
    await page.getByTestId('theme-toggle').click();
    await expect(html).not.toHaveClass(/dark/);
    expect(await page.evaluate(() => localStorage.getItem('ci-theme'))).toBe('light');
    await page.reload();
    await expect(html).not.toHaveClass(/dark/);
    await page.getByTestId('theme-toggle').click();
    await expect(html).toHaveClass(/dark/);
  });

  test('sem flash: o tema escolhido ja esta aplicado antes de o app carregar', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('ci-theme', 'light'));
    await page.route('**/src/main.tsx*', (route) => route.abort());
    await page.goto('/dashboard');
    await expect(page.locator('html')).not.toHaveClass(/dark/);
    expect(await page.evaluate(() => document.documentElement.style.colorScheme)).toBe('light');
    const bg = await page.evaluate(
      () => getComputedStyle(document.documentElement).backgroundColor,
    );
    expect(bg).toBe('rgb(243, 245, 248)');
  });

  test('cores dos tokens nos dois temas', async ({ page }) => {
    await openApp(page, '/dashboard');
    const token = (name: string) =>
      page.evaluate(
        (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(),
        name,
      );
    expect(await token('--bg')).toBe('#0b0f14');
    expect(await token('--live-fill')).toBe('#b91c1c');
    await page.getByTestId('theme-toggle').click();
    expect(await token('--bg')).toBe('#f3f5f8');
    expect(await token('--warn')).toBe('#8a4b00');
  });
});
