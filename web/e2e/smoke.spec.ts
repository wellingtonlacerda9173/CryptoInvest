import { expect, test } from '@playwright/test';
import { ADMIN, openApp, seedSession } from './helpers';

test.describe('autenticacao e navegacao', () => {
  test('rota protegida sem sessao redireciona para o login', async ({ page }) => {
    await page.goto('/dashboard');
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole('heading', { name: 'Entre para ver o seu robô' })).toBeVisible();
  });

  test('login em dois passos, F5 mantem a sessao e sair volta ao login', async ({ page }) => {
    await page.goto('/dashboard');
    await page.getByLabel('Usuário').fill(ADMIN.username);
    await page.getByLabel('Senha', { exact: true }).fill(ADMIN.password);
    await page.getByRole('button', { name: 'Continuar' }).click();
    await page.getByLabel('Código do aplicativo autenticador').fill(ADMIN.code);
    await expect(page).toHaveURL(/\/dashboard$/);
    await expect(page.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeVisible();

    await page.reload();
    await expect(page.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeVisible();
    await expect(page).toHaveURL(/\/dashboard$/);

    await page.getByRole('button', { name: 'Sair' }).click();
    await expect(page).toHaveURL(/\/login$/);
  });

  test('credenciais erradas mostram mensagem generica', async ({ page }) => {
    await page.goto('/login');
    await page.getByLabel('Usuário').fill('alguem');
    await page.getByLabel('Senha', { exact: true }).fill('errada');
    await page.getByRole('button', { name: 'Continuar' }).click();
    await expect(page.getByRole('alert')).toContainText('não conferem');
  });

  test('navega por todas as telas do menu', async ({ page }) => {
    await seedSession(page);
    await openApp(page, '/dashboard');
    const nav = page.getByRole('navigation', { name: 'Principal', exact: true });
    const titles: [string, string][] = [
      ['Controle', 'Controle do robô'],
      ['Mercado', 'Mercado'],
      ['Decisões ao vivo', 'Decisões ao vivo'],
      ['Operações', 'Operações'],
      ['Desempenho', 'Desempenho'],
      ['Backtest', 'Backtest'],
      ['Configurações', 'Configurações'],
      ['Dashboard', 'Dashboard'],
    ];
    for (const [link, h1] of titles) {
      await nav.getByRole('link', { name: link }).click();
      await expect(page.getByRole('heading', { level: 1, name: h1 })).toBeVisible();
      await expect(nav.getByRole('link', { name: link })).toHaveAttribute('aria-current', 'page');
    }
  });

  test('onboarding, kit e rota inexistente', async ({ page }) => {
    await seedSession(page);
    await openApp(page, '/onboarding');
    await expect(page.getByText('Passo 1 de 3')).toBeVisible();
    await openApp(page, '/_kit');
    await expect(page.getByRole('heading', { level: 1, name: 'Kit de componentes' })).toBeVisible();
    await page.goto('/nao-existe');
    await expect(
      page.getByRole('heading', { name: 'Página não encontrada' }).first(),
    ).toBeVisible();
  });

  test('leitor nao pode parar o robo', async ({ page }) => {
    await seedSession(page, 'reader');
    await openApp(page, '/dashboard');
    const stop = page.getByRole('button', { name: 'Parar tudo' });
    await expect(stop).toHaveAttribute('aria-disabled', 'true');
    await stop.click({ force: true });
    await expect(page.getByRole('alertdialog')).toHaveCount(0);
  });
});
