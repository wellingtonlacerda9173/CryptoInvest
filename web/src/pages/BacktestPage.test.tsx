import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { makeJob, makeReport, makeWalkForward } from '@/test/backtestData';
import { renderPage, stubApi } from '@/test/render';
import BacktestPage from './BacktestPage';

vi.mock('@/components/kit/EquityChart', () => ({
  EquityChart: () => <div role="img" aria-label="Curva de capital simulada" />,
}));

const ID = 'a'.repeat(32);
const empty = { items: [], next_cursor: null };

async function next() {
  await userEvent.click(screen.getByRole('button', { name: /Continuar/ }));
}

describe('Backtest: assistente', () => {
  it('vazio no historico e etapa 1 com o selo BACKTEST', async () => {
    stubApi({ 'GET /backtests': () => empty });
    renderPage(<BacktestPage />);
    expect(await screen.findByText('Nenhum backtest ainda')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Etapa 1 de 5: Par' })).toBeInTheDocument();
    expect(screen.getAllByText('BACKTEST').length).toBeGreaterThan(0);
  });

  it('nao avanca com erro: periodo antes de 2017 e fim no futuro', async () => {
    stubApi({ 'GET /backtests': () => empty });
    renderPage(<BacktestPage />);
    await next();
    await userEvent.click(screen.getByRole('radio', { name: 'Datas escolhidas por mim' }));
    await next();
    expect(screen.getByText('Escolha a data inicial.')).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Data inicial'), '2017-01-01');
    await userEvent.type(screen.getByLabelText('Data final'), '2018-01-01');
    await next();
    expect(screen.getByText(/17\/08\/2017/, { selector: 'span' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Etapa 2 de 5/ })).toBeInTheDocument();
  });

  it('premissas fora da faixa aparecem por campo', async () => {
    stubApi({ 'GET /backtests': () => empty });
    renderPage(<BacktestPage />);
    await next();
    await next();
    await next();
    await userEvent.type(screen.getByRole('textbox', { name: /Confiança mínima/ }), '1.5');
    await userEvent.clear(screen.getByLabelText(/Capital inicial/));
    await next();
    expect(screen.getByText('Preencha este campo.')).toBeInTheDocument();
    expect(screen.getByText('Confiança: de 0 a 1.')).toBeInTheDocument();
  });

  it('roda: envia o pedido da API e passa a acompanhar o progresso; cancelar chama a API', async () => {
    const api = stubApi({
      'GET /backtests': () => empty,
      'POST /backtests': () => makeJob({ status: 'queued' }),
      [`GET /backtests/${ID}`]: () => makeJob(),
      [`POST /backtests/${ID}/cancel`]: () => makeJob({ cancel_requested: true }),
    });
    renderPage(<BacktestPage />);
    for (let i = 0; i < 4; i++) await next();
    expect(screen.getByText(/Pares:/)).toHaveTextContent('BTC/USDT');
    await userEvent.click(screen.getByRole('button', { name: 'Rodar backtest' }));

    const bar = await screen.findByRole('progressbar', { name: 'Progresso do backtest' });
    expect(bar).toHaveAttribute('aria-valuenow', '42');
    expect(screen.getByText('Simulando as operações')).toBeInTheDocument();
    const [post] = api.find('POST', '/backtests');
    expect(post?.body).toMatchObject({
      symbols: ['BTC/USDT'],
      timeframe: '1h',
      years: 1,
      strategies: ['elephant', 'ema'],
      capital: 10000,
      taker_fee: 0.001,
      walk_forward: false,
    });
    await userEvent.click(screen.getByRole('button', { name: 'Cancelar o teste' }));
    await waitFor(() => expect(api.find('POST', `/backtests/${ID}/cancel`)).toHaveLength(1));
  });

  it('leitor ve o assistente, mas nao roda nem cancela', async () => {
    stubApi({
      'GET /backtests': () => empty,
      [`GET /backtests/${ID}`]: () => makeJob(),
    });
    renderPage(<BacktestPage />, { role: 'reader' });
    for (let i = 0; i < 4; i++) await next();
    expect(screen.getByRole('button', { name: 'Rodar backtest' })).toBeDisabled();
    expect(screen.getByText(/Só administradores iniciam backtests/)).toBeInTheDocument();
  });

  it('erro da API ao criar e mostrado e nada navega', async () => {
    stubApi({
      'GET /backtests': () => empty,
      'POST /backtests': () =>
        new Response(
          JSON.stringify({
            type: 'x/validation-error',
            title: 'Dados inválidos',
            status: 422,
            detail: 'O período máximo é de 5 anos por backtest.',
          }),
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
    });
    renderPage(<BacktestPage />);
    for (let i = 0; i < 4; i++) await next();
    await userEvent.click(screen.getByRole('button', { name: 'Rodar backtest' }));
    expect(
      await screen.findByText('O período máximo é de 5 anos por backtest.'),
    ).toBeInTheDocument();
  });
});

describe('Backtest: relatorio', () => {
  const done = makeJob({
    status: 'succeeded',
    has_report: true,
    finished_at: '2026-10-24T12:05:00Z',
  });

  it('risco e consistencia primeiro, amostra pequena, evitadas por tipo e premissas', async () => {
    stubApi({
      'GET /backtests': () => empty,
      [`GET /backtests/${ID}`]: () => done,
      [`GET /backtests/${ID}/report`]: () => makeReport({ trades: 12 }),
    });
    renderPage(<BacktestPage />, { path: `/?teste=${ID}` });
    const risk = await screen.findByRole('region', { name: 'Risco e consistência' });
    const result = screen.getByRole('region', { name: 'Resultado' });
    expect(risk.compareDocumentPosition(result) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(risk).getByText('3,2%')).toBeInTheDocument();
    expect(screen.getByText(/Amostra pequena\./).closest('[role="note"]')).toHaveTextContent(
      '12 operações neste teste',
    );
    expect(
      screen.getByText('Resultado de simulação no passado. Não garante o futuro.'),
    ).toBeInTheDocument();
    for (const t of ['Capacidade', 'Travas do robô', 'Filtros de mercado e risco'])
      expect(screen.getByText(t, { selector: 'strong' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Operações evitadas' })).toHaveTextContent(
      /a soma não é o efeito real no saldo/,
    );
    expect(screen.getByText(/sem conferência em janelas/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Premissas e limitações/ })).toBeInTheDocument();
    expect(
      screen.getByText('(perda)', { exact: false, selector: 'span.sr-only' }),
    ).toBeInTheDocument();
  });

  it('walk-forward: veredito honesto e aviso de amostra pequena fora da amostra', async () => {
    stubApi({
      'GET /backtests': () => empty,
      [`GET /backtests/${ID}`]: () => done,
      [`GET /backtests/${ID}/report`]: () =>
        makeReport({ trades: 40, walk: makeWalkForward('inconclusivo (amostra pequena)', 8) }),
    });
    renderPage(<BacktestPage />, { path: `/?teste=${ID}` });
    const verdict = await screen.findByTestId('veredito');
    expect(verdict).toHaveTextContent('Inconclusivo');
    expect(verdict).toHaveTextContent('Apenas 8 operações fora da amostra');
    expect(
      screen.getByText(/8 operações fora da amostra: poucas para concluir/),
    ).toBeInTheDocument();
  });

  it.each([
    ['sem edge detectavel fora da amostra', 'Sem vantagem detectável'],
    ['indicios de sobreajuste', 'Indícios de sobreajuste'],
    ['consistente', 'Consistente'],
  ])('veredito %s', async (raw, label) => {
    stubApi({
      'GET /backtests': () => empty,
      [`GET /backtests/${ID}`]: () => done,
      [`GET /backtests/${ID}/report`]: () =>
        makeReport({ trades: 40, walk: makeWalkForward(raw, 50) }),
    });
    renderPage(<BacktestPage />, { path: `/?teste=${ID}` });
    expect(await screen.findByTestId('veredito')).toHaveTextContent(label);
  });

  it('job que falhou mostra o motivo e permite refazer', async () => {
    stubApi({
      'GET /backtests': () => empty,
      [`GET /backtests/${ID}`]: () => makeJob({ status: 'failed', error: 'sem velas suficientes' }),
    });
    renderPage(<BacktestPage />, { path: `/?teste=${ID}` });
    expect(await screen.findByRole('alert')).toHaveTextContent('sem velas suficientes');
    await userEvent.click(screen.getByRole('button', { name: /Refazer com outras premissas/ }));
    expect(await screen.findByRole('heading', { name: /Etapa 1 de 5/ })).toBeInTheDocument();
  });
});
