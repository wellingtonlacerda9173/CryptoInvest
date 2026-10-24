import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { ThemeToggle } from '@/components/layout/ThemeToggle';
import { ThemeProvider } from './ThemeProvider';
import { applyTheme, readStoredTheme, THEME_KEY } from './theme';

const html = readFileSync(resolve(__dirname, '../../index.html'), 'utf8');
const antiFlash = /<script>([\s\S]*?)<\/script>/.exec(html)![1]!;

const runAntiFlash = () => new Function(antiFlash)();

describe('tema', () => {
  it('o padrao e escuro', () => {
    expect(readStoredTheme()).toBe('dark');
  });

  it('script anti-flash: sem escolha salva, mantem escuro', () => {
    document.documentElement.className = '';
    runAntiFlash();
    expect(document.documentElement).toHaveClass('dark');
    expect(document.documentElement.style.colorScheme).toBe('dark');
  });

  it('script anti-flash: aplica o tema claro salvo antes do React', () => {
    localStorage.setItem(THEME_KEY, 'light');
    document.documentElement.className = 'dark';
    runAntiFlash();
    expect(document.documentElement).not.toHaveClass('dark');
    expect(document.documentElement.style.colorScheme).toBe('light');
  });

  it('script anti-flash ignora valores invalidos', () => {
    localStorage.setItem(THEME_KEY, 'neon');
    runAntiFlash();
    expect(document.documentElement).toHaveClass('dark');
  });

  it('o HTML ja nasce escuro e o script roda antes do bundle', () => {
    expect(html).toMatch(/<html[^>]*class="dark"/);
    expect(html.indexOf('localStorage')).toBeLessThan(html.indexOf('src="/src/main.tsx"'));
  });

  it('alternar em um clique aplica a classe e persiste', async () => {
    const user = userEvent.setup();
    applyTheme('dark');
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    await user.click(screen.getByRole('button', { name: 'Tema escuro / claro' }));
    expect(document.documentElement).not.toHaveClass('dark');
    expect(localStorage.getItem(THEME_KEY)).toBe('light');
    await user.click(screen.getByRole('button', { name: 'Tema escuro / claro' }));
    expect(document.documentElement).toHaveClass('dark');
    expect(localStorage.getItem(THEME_KEY)).toBe('dark');
  });

  it('lembra a escolha ao montar de novo', () => {
    localStorage.setItem(THEME_KEY, 'light');
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    expect(document.documentElement).not.toHaveClass('dark');
  });
});
