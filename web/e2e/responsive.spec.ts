import { expect, test } from '@playwright/test';
import { expectNoHorizontalScroll, openApp, seedSession, VIEWPORTS, WIDTHS } from './helpers';

test.describe('responsividade', () => {
  test.beforeEach(async ({ page }) => seedSession(page));

  for (const width of WIDTHS) {
    test(`layout em ${width}px`, async ({ page }) => {
      await page.setViewportSize(VIEWPORTS[width]);
      await openApp(page, '/dashboard');
      await expectNoHorizontalScroll(page);

      const stop = page.getByRole('button', { name: 'Parar tudo' });
      await expect(stop).toBeVisible();
      const box = (await stop.boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(44);

      const side = page.locator('aside');
      const bottom = page.getByRole('navigation', { name: 'Principal (celular)' });
      if (width === 390) {
        await expect(side).toBeHidden();
        await expect(bottom).toBeVisible();
      } else {
        await expect(side).toBeVisible();
        await expect(bottom).toBeHidden();
        const w = (await side.boundingBox())!.width;
        expect(Math.round(w)).toBe(width === 1440 ? 248 : 72);
      }

      await page.evaluate(() => window.scrollTo(0, 400));
      await expect(stop).toBeInViewport();
    });
  }

  test('menu lateral recolhe e lembra a escolha (desktop)', async ({ page }) => {
    await page.setViewportSize(VIEWPORTS[1440]);
    await openApp(page, '/dashboard');
    await page.getByRole('button', { name: 'Recolher menu' }).click();
    expect(Math.round((await page.locator('aside').boundingBox())!.width)).toBe(72);
    await page.reload();
    await expect(page.getByRole('button', { name: 'Expandir menu' })).toBeVisible();
  });

  test('faixa de desconexao aparece, esmaece os numeros e some ao reconectar', async ({ page }) => {
    await openApp(page, '/dashboard');
    await expect(page.getByTestId('connection-banner')).toHaveCount(0);
    await page.evaluate(() => window.__ciMock?.dropConnection());
    await expect(page.getByTestId('connection-banner')).toContainText('Sem conexão');
    await expect(
      page.getByRole('status').filter({ hasText: 'SEM CONEXÃO COM O PAINEL' }),
    ).toBeVisible();
    await expect(page.getByText('desatualizado', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Parar tudo' })).toBeVisible();
    await page.evaluate(() => window.__ciMock?.restoreConnection());
    await expect(page.getByTestId('connection-banner')).toHaveCount(0, { timeout: 20_000 });
  });

  test('LIVE: selo cheio e moldura vermelha; estados do robo na barra', async ({ page }) => {
    await openApp(page, '/dashboard');
    await page.evaluate(() =>
      window.__ciMock?.setStatus({
        mode: 'live',
        simulated: false,
        equity: 2540.18,
        day_pnl: 12.4,
        day_pnl_pct: 0.49,
      }),
    );
    await expect(page.locator('header [data-mode="live"]')).toContainText('dinheiro real');
    await expect(page.locator('body')).toHaveAttribute('data-modo', 'live');
    await expect(page.getByText('Saldo (real)')).toBeVisible();
    await page.evaluate(() => window.__ciMock?.setStatus({ state: 'HALTED' }));
    await expect(
      page.getByRole('status').filter({ hasText: 'DESLIGADO por drawdown' }),
    ).toBeVisible();
  });
});
