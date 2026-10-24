import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { makeAccount, makeStatus } from '@/mocks/mockState';
import { renderPage, stubApi } from '@/test/render';
import { TopBar } from './TopBar';

describe('TopBar', () => {
  // Uma grandeza, uma fonte: /status mede o dia pela conta de risco do robô e pode divergir de /account.
  it('mostra o resultado do dia e o saldo de /account, ignorando o day_pnl de /status', async () => {
    stubApi({
      'GET /status': () => makeStatus({ day_pnl: 0, day_pnl_pct: 0, equity: 1 }),
      'GET /account/summary': () =>
        makeAccount({ equity: 10_000, day_pnl: -198.8, day_pnl_pct: -1.95 }),
    });
    renderPage(<TopBar />);
    const day = await screen.findByText(/−198,80/);
    expect(day).toHaveTextContent('−1,95%');
    expect(screen.queryByText(/^0,00/)).not.toBeInTheDocument();
  });
});
