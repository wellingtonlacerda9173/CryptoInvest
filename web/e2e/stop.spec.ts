import { expect, test } from '@playwright/test';
import { openApp, seedSession } from './helpers';

test.describe('Parar tudo', () => {
  test.beforeEach(async ({ page }) => seedSession(page));

  test('abre o dialogo, padrao seguro, Esc cancela e devolve o foco', async ({ page }) => {
    await openApp(page, '/dashboard');
    const trigger = page.getByRole('button', { name: 'Parar tudo' });
    await trigger.click();
    const dialog = page.getByRole('alertdialog', { name: 'Parar tudo agora?' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('radio', { name: /Só bloquear novas operações/ })).toBeChecked();
    await expect(dialog.getByLabel('Motivo')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await expect(page.getByRole('status').filter({ hasText: 'Operando' }).first()).toBeVisible();
  });

  test('confirmar so bloqueia novas operacoes e atualiza o estado', async ({ page }) => {
    await openApp(page, '/dashboard');
    await page.getByRole('button', { name: 'Parar tudo' }).click();
    await page.getByRole('button', { name: 'Confirmar: parar tudo' }).click();
    await expect(page.getByText(/Parada: |Parada enviada ao robô/).first()).toBeVisible();
    await expect(page.getByRole('alertdialog')).toHaveCount(0);
    await expect(page.getByRole('status').filter({ hasText: 'Pausado' }).first()).toBeVisible();
  });

  test('vender tudo exige nome e motivo', async ({ page }) => {
    await openApp(page, '/dashboard');
    await page.getByRole('button', { name: 'Parar tudo' }).click();
    const dialog = page.getByRole('alertdialog');
    await dialog.getByRole('radio', { name: /vender tudo/ }).check();
    const confirm = dialog.getByRole('button', { name: 'Confirmar: parar tudo' });
    await expect(confirm).toBeDisabled();
    await dialog.getByLabel('Motivo').fill('Notícia inesperada');
    await expect(confirm).toBeEnabled();
    await dialog.getByLabel('Seu nome (fica no registro)').fill('');
    await expect(confirm).toBeDisabled();
  });
});
