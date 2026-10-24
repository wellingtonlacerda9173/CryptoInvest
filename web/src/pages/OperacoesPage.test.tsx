import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { makePositions } from '@/mocks/mockState';
import { LOSS, makeCandles, makeTrade, makeTradePage, SETTINGS } from '@/test/marketData';
import { problem, renderPage, stubApi } from '@/test/render';
import OperacoesPage from './OperacoesPage';

vi.mock('@/components/kit/CandleChart', () => ({
  CandleChart: (p: { levels?: { title: string }[]; label: string }) => (
    <div data-testid="grafico-trade" role="img" aria-label={p.label}>
      {p.levels?.map((l) => l.title).join(',')}
    </div>
  ),
}));

const base = { 'GET /settings': () => SETTINGS };

async function openClosed() {
  await userEvent.click(await screen.findByRole('tab', { name: 'Encerradas' }));
}

describe('Operacoes', () => {
  it('abertas: tabela com posicao, e vazio explicativo quando nao ha', async () => {
    stubApi({ ...base });
    renderPage(<OperacoesPage />);
    expect(await screen.findByRole('region', { name: 'Posições abertas' })).toBeInTheDocument();
    expect(makePositions().length).toBeGreaterThan(0);
    stubApi({ ...base, 'GET /positions': () => [] });
    renderPage(<OperacoesPage />);
    expect(
      await screen.findByText(/Ficar parado em USDT é uma decisão válida/),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Ver decisões ao vivo' })).toBeInTheDocument();
  });

  it('encerradas: perdas sempre aparecem, com icone e texto alem da cor', async () => {
    stubApi({
      ...base,
      'GET /trades': () => makeTradePage([makeTrade(1), LOSS(2), makeTrade(3)]),
    });
    renderPage(<OperacoesPage />);
    await openClosed();
    const resumo = await screen.findByTestId('resumo-operacoes');
    expect(resumo).toHaveTextContent('1 perdas');
    const rows = screen.getAllByRole('row').filter((r) => r.getAttribute('data-result'));
    expect(rows).toHaveLength(3);
    const loss = rows.find((r) => r.getAttribute('data-result') === 'loss')!;
    expect(within(loss).getByText('Perda')).toBeInTheDocument();
    expect(loss).toHaveTextContent('−50,00');
    expect(screen.queryByRole('option', { name: 'Só ganhos' })).not.toBeInTheDocument();
  });

  it('filtros e busca vao para a API (busca com atraso)', async () => {
    const api = stubApi({ ...base, 'GET /trades': () => makeTradePage([makeTrade(1)]) });
    renderPage(<OperacoesPage />);
    await openClosed();
    await screen.findByTestId('resumo-operacoes');
    await userEvent.selectOptions(screen.getByLabelText('Resultado'), 'loss');
    await userEvent.selectOptions(screen.getByLabelText('Par'), 'ETH/USDT');
    await userEvent.type(screen.getByLabelText('Buscar'), 'elephant');
    await waitFor(() => {
      const s = api.find('GET', '/trades').at(-1)?.search ?? '';
      expect(s).toContain('result=loss');
      expect(s).toContain('symbol=ETH%2FUSDT');
      expect(s).toContain('q=elephant');
    });
  });

  it('pagina por cursor', async () => {
    const api = stubApi({
      ...base,
      'GET /trades': (_b, url) =>
        url.searchParams.get('cursor') === '7'
          ? makeTradePage([makeTrade(8)])
          : makeTradePage([makeTrade(9)], 7),
    });
    renderPage(<OperacoesPage />);
    await openClosed();
    await userEvent.click(await screen.findByRole('button', { name: 'Carregar mais operações' }));
    await waitFor(() =>
      expect(screen.getAllByRole('row').filter((r) => r.dataset.result)).toHaveLength(2),
    );
    expect(api.find('GET', '/trades').at(-1)?.search).toContain('cursor=7');
  });

  it('detalhe abre o grafico do momento com entrada e saida', async () => {
    stubApi({
      ...base,
      'GET /trades': () => makeTradePage([LOSS(1)]),
      'GET /market/candles': () => makeCandles(),
    });
    renderPage(<OperacoesPage />);
    await openClosed();
    await userEvent.click(await screen.findByRole('button', { name: /Ver detalhes da operação/ }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByTestId('grafico-trade')).toHaveTextContent('Entrada,Stop');
    expect(within(dialog).getByText('Perda')).toBeInTheDocument();
  });

  it('exporta pelo endpoint da API (CSV nao e montado no cliente)', async () => {
    const api = stubApi({
      ...base,
      'GET /trades': () => makeTradePage([makeTrade(1)]),
      'GET /trades/export': () =>
        new Response('﻿id\n1\n', {
          headers: {
            'Content-Type': 'text/csv',
            'Content-Disposition': 'attachment; filename="operacoes-paper.csv"',
          },
        }),
    });
    URL.createObjectURL = vi.fn(() => 'blob:x');
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    renderPage(<OperacoesPage />);
    await openClosed();
    await userEvent.selectOptions(await screen.findByLabelText('Resultado'), 'loss');
    await userEvent.click(screen.getByRole('button', { name: 'Exportar CSV' }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    const call = api.find('GET', '/trades/export').at(-1);
    expect(call?.search).toContain('mode=paper');
    expect(call?.search).toContain('result=loss');
  });

  it('vazio, erro e leitor (tela e somente leitura)', async () => {
    stubApi({ ...base, 'GET /trades': () => makeTradePage([]) });
    renderPage(<OperacoesPage />, { role: 'reader' });
    await openClosed();
    expect(await screen.findByText('Nenhuma operação encerrada ainda')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Exportar CSV' })).toBeDisabled();

    stubApi({ ...base, 'GET /trades': () => problem(500, 'Falha') });
    renderPage(<OperacoesPage />, { role: 'reader' });
    await userEvent.click((await screen.findAllByRole('tab', { name: 'Encerradas' })).at(-1)!);
    expect((await screen.findAllByRole('alert')).length).toBeGreaterThan(0);
  });
});
