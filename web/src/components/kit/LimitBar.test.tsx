import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LimitBar, levelFor } from './LimitBar';

const bar = (value: number, limit = 100, extra = {}) =>
  render(
    <LimitBar
      name="Perda diária"
      label="Perda diária"
      valueText={`${value} de ${limit}`}
      value={value}
      limit={limit}
      {...extra}
    />,
  );

describe('LimitBar', () => {
  it('limiares: ate 60% folgado, 60-90% atencao, 90% ou mais no limite', () => {
    expect(levelFor(0.59)).toBe('ok');
    expect(levelFor(0.6)).toBe('warn');
    expect(levelFor(0.89)).toBe('warn');
    expect(levelFor(0.9)).toBe('bad');
  });

  it.each([
    [30, 'Folgado'],
    [75, 'Atenção'],
    [95, 'No limite'],
  ])('%s%% mostra a palavra "%s" alem da cor', (value, word) => {
    const { container } = bar(value);
    expect(screen.getByText(word)).toBeInTheDocument();
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });

  it('expoe progressbar com valores e nome acessivel', () => {
    bar(30, 100);
    const pb = screen.getByRole('progressbar', { name: 'Perda diária' });
    expect(pb).toHaveAttribute('aria-valuenow', '30');
    expect(pb).toHaveAttribute('aria-valuemin', '0');
    expect(pb).toHaveAttribute('aria-valuemax', '100');
    expect(screen.getByRole('group')).toHaveAccessibleName('Perda diária: 30 de 100. Folgado');
  });

  it('aceita nivel e texto de estado personalizados', () => {
    bar(1, 2, { level: 'warn', stateText: 'Atenção: 1 perda a menos do limite' });
    expect(screen.getByText('Atenção: 1 perda a menos do limite')).toBeInTheDocument();
  });

  it('nunca passa de 100% na barra', () => {
    bar(300, 100);
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100');
  });
});
