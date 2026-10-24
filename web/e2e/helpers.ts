import AxeBuilder from '@axe-core/playwright';
import { expect, type Page } from '@playwright/test';
import { MOCK_PASSWORD, MOCK_TOTP, MOCK_USERS } from '../src/mocks/mockState';

export const VIEWPORTS = {
  1440: { width: 1440, height: 900 },
  820: { width: 820, height: 1180 },
  390: { width: 390, height: 844 },
} as const;
export type Width = keyof typeof VIEWPORTS;
export const WIDTHS = Object.keys(VIEWPORTS).map(Number) as Width[];
export const THEMES = ['escuro', 'claro'] as const;
export type ThemeName = (typeof THEMES)[number];

export const ADMIN = {
  username: MOCK_USERS.admin.username,
  password: MOCK_PASSWORD,
  code: MOCK_TOTP,
};

export async function seedSession(page: Page, role: 'admin' | 'reader' = 'admin') {
  await page.addInitScript((r) => sessionStorage.setItem('ci-mock-session', r), role);
}

export async function seedTheme(page: Page, theme: ThemeName) {
  await page.addInitScript(
    (t) => localStorage.setItem('ci-theme', t === 'claro' ? 'light' : 'dark'),
    theme,
  );
}

/** Abre a rota e espera o painel mostrar dados do robo (nao o esqueleto de carregamento). */
export async function openApp(page: Page, path: string) {
  await page.goto(path);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  if (path !== '/login' && path !== '/onboarding') {
    await expect(page.locator('header[data-mode="paper"]').first()).toBeVisible();
    await expect(page.getByRole('status').filter({ hasText: 'Operando' }).first()).toBeVisible();
  }
}

export async function axeScan(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice'])
    .analyze();
  return results.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    nodes: v.nodes
      .slice(0, 3)
      .map((n) => `${n.target.join(' ')} :: ${(n.failureSummary ?? '').split('\n')[1] ?? ''}`),
  }));
}

export async function expectNoHorizontalScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}
