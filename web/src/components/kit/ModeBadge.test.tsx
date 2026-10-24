import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { TradingMode } from '@/lib/modes';
import { ModeBadge } from './ModeBadge';

const MODES: TradingMode[] = ['backtest', 'paper', 'live'];

describe('ModeBadge', () => {
  it.each([
    ['backtest', 'BACKTEST', 'histórico', 'border-dotted'],
    ['paper', 'PAPER', 'simulado', 'border-dashed'],
    ['live', 'LIVE', 'dinheiro real', 'border-solid'],
  ] as const)('%s: palavra, subtexto e forma da borda', (mode, word, hint, border) => {
    const { container } = render(<ModeBadge mode={mode} />);
    expect(screen.getByText(word)).toBeInTheDocument();
    expect(screen.getByText(hint)).toBeInTheDocument();
    expect(container.firstElementChild).toHaveClass(border);
  });

  it('nunca e ambiguo: cada modo tem palavra, borda e cor exclusivas', () => {
    const rendered = MODES.map((mode) => {
      const { container, unmount } = render(<ModeBadge mode={mode} />);
      const el = container.firstElementChild as HTMLElement;
      const snapshot = { text: el.textContent, cls: el.className };
      unmount();
      return snapshot;
    });
    expect(new Set(rendered.map((r) => r.text)).size).toBe(3);
    expect(new Set(rendered.map((r) => r.cls)).size).toBe(3);
  });

  it('LIVE e o unico com preenchimento vermelho cheio e halo', () => {
    const live = render(<ModeBadge mode="live" />).container.firstElementChild;
    expect(live).toHaveClass('bg-live-fill', 'text-live-text');
    expect(live?.className).toContain('shadow-');
    for (const mode of ['paper', 'backtest'] as const) {
      const other = render(<ModeBadge mode={mode} />).container.firstElementChild;
      expect(other).not.toHaveClass('bg-live-fill');
    }
  });

  it('no celular o subtexto continua para leitores de tela', () => {
    render(<ModeBadge mode="paper" hintOnMobile={false} />);
    expect(screen.getByText('simulado')).toHaveClass('max-sm:sr-only');
  });
});
