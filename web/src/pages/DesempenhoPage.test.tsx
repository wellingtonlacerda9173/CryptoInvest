import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { makeEquity } from '@/mocks/mockState';
import { LOSS, makePerformance, makeTrade, makeTradePage } from '@/test/marketData';
import { problem, renderPage, stubApi } from '@/test/render';
import DesempenhoPage from './DesempenhoPage';
import { binR } from './desempenho/binR';

const routes = (perf = makePerformance()) => ({
  'GET /performance': () => perf,
  'GET /trades': () => makeTradePage([makeTrade(1), LOSS(2)]),
  'GET /equity': () => makeEquity(),
});

describe('Desempenho', () => {
  it('mostra KPIs com tooltip, aviso de amostra pequena e quebras', async () => {
    stubApi(routes());
    renderPage(<DesempenhoPage />);
    const kpis = await screen.findByRole('region', { name: 'Indicadores de desempenho' });
    await waitFor(() => expect(within(kpis).getByText('2,3%')).toBeInTheDocument());
    expect(within(kpis).getByText('56%')).toBeInTheDocument();
    for (const term of ['Drawdown máximo', 'Profit factor', 'Payoff', 'Sharpe', 'Taxa de acerto']) {
      expect(within(kpis).getByRole('button', { name: term })).toBeInTheDocument();
    }
    expect(screen.getByText(/poucas para concluir/)).toBeInTheDocument();
    expect(
      await screen.findByRole('region', { name: 'Resultado por estratégia: tabela' }),
    ).toHaveTextContent('elephant');
    expect(screen.getByRole('region', { name: 'Resultado por mês: tabela' })).toHaveTextContent(
      '10/2026',
    );
    expect(screen.getByText(/Perto de notícia importante/)).toBeInTheDocument();
    expect(screen.getByText(/não registra o regime/)).toBeInTheDocument();
  });

  it('perda por par aparece com sinal negativo e seta', async () => {
    stubApi(routes());
    renderPage(<DesempenhoPage />);
    const pair = await screen.findByRole('region', { name: 'Resultado por par' });
    expect(await within(pair).findByText('−30,00')).toBeInTheDocument();
  });

  it('sem operacoes: estado vazio honesto, sem graficos', async () => {
    stubApi(
      routes(
        makePerformance({
          trades: 0,
          wins: 0,
          losses: 0,
          win_rate: null,
          profit_factor: null,
          payoff: null,
          sharpe: null,
          expectancy_r: null,
          by_month: [],
          by_pair: [],
          by_strategy: [],
          avoided: [],
          sample_warning:
            'Ainda sem dados suficientes: os gráficos aparecem após a 1ª operação encerrada.',
        }),
      ),
    );
    renderPage(<DesempenhoPage />);
    expect(await screen.findByText('Ainda sem dados suficientes')).toBeInTheDocument();
    expect(screen.queryByText('Curva de capital')).not.toBeInTheDocument();
    expect(screen.getByText(/Nenhuma oportunidade recusada/)).toBeInTheDocument();
  });

  it('periodo vai para a API', async () => {
    const api = stubApi(routes());
    renderPage(<DesempenhoPage />);
    await screen.findByRole('region', { name: 'Indicadores de desempenho' });
    await userEvent.selectOptions(screen.getByLabelText('Período'), '90');
    await waitFor(() => expect(api.find('GET', '/performance').at(-1)?.search).toBe('?days=90'));
    await userEvent.selectOptions(screen.getByLabelText('Período'), 'all');
    await waitFor(() => expect(api.find('GET', '/performance').at(-1)?.search).toBe(''));
  });

  it('erro de performance mostra card de erro e a tela segue', async () => {
    stubApi({ ...routes(), 'GET /performance': () => problem(500, 'Falha') });
    renderPage(<DesempenhoPage />);
    expect((await screen.findAllByRole('alert')).length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { level: 1, name: 'Desempenho' })).toBeInTheDocument();
  });
});

describe('histograma de R', () => {
  it('agrupa em faixas de 0,5 R e separa perdas de ganhos', () => {
    const bins = binR([-1, -1, 0.2, 2, 2.1]);
    expect(bins[0]).toEqual({ from: -1, count: 2 });
    expect(bins.find((b) => b.from === 0)?.count).toBe(1);
    expect(bins.find((b) => b.from === 2)?.count).toBe(2);
    expect(bins.reduce((s, b) => s + b.count, 0)).toBe(5);
    expect(binR([])).toEqual([]);
  });
});
