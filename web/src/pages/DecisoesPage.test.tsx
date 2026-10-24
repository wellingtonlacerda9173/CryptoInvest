import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Decision } from '@/api/schemas';
import { makeDecision, SETTINGS } from '@/test/marketData';
import { problem, renderPage, stubApi } from '@/test/render';
import DecisoesPage from './DecisoesPage';

const page = (items: Decision[], next: number | null = null) => ({
  items,
  next_cursor: next,
  summary: { day: '2026-10-24', entries: 1, refusals: 2, signals: 3, waits: 4 },
});

describe('Decisoes ao vivo', () => {
  it('lista a timeline com resumo do dia e expande o detalhe com regime, scores, IA e decisao final', async () => {
    const entered = makeDecision(1, {
      outcome: 'Entrou',
      outcome_kind: 'entered',
      traded: true,
      action: 'enter',
      reason_text: 'Barra elefante com rompimento.',
      strategy: 'elephant',
      confidence: 0.7,
      signal: {
        regime: 'tendência de alta',
        scores: [
          { strategy: 'elephant', score: 0.7 },
          { strategy: 'ema', score: 0.4 },
        ],
        ai_opinion: 'Contexto favorável.',
        entry: '100',
        stop_loss: '95',
        take_profit: '110',
      },
    });
    stubApi({
      'GET /settings': () => SETTINGS,
      'GET /decisions': () => page([entered, makeDecision(2)]),
    });
    renderPage(<DecisoesPage />);
    expect(await screen.findByTestId('resumo-dia')).toHaveTextContent('3 sinais');
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    const first = items[0]!;
    await userEvent.click(within(first).getByRole('button', { name: /Detalhes/ }));
    expect(within(first).getByText('tendência de alta')).toBeInTheDocument();
    expect(within(first).getByText('elephant: nota 0,70')).toBeInTheDocument();
    expect(within(first).getByText('ema: nota 0,40')).toBeInTheDocument();
    expect(within(first).getByText('Contexto favorável.')).toBeInTheDocument();
    expect(within(first).getByText(/O robô entrou na operação/)).toBeInTheDocument();
  });

  it('sem regime e sem IA, explica o vazio em vez de esconder', async () => {
    stubApi({ 'GET /settings': () => SETTINGS, 'GET /decisions': () => page([makeDecision(1)]) });
    renderPage(<DecisoesPage />);
    await userEvent.click(await screen.findByRole('button', { name: /Detalhes/ }));
    expect(screen.getByText(/ainda não registra o regime/)).toBeInTheDocument();
    expect(screen.getByText(/Sem parecer da IA nesta decisão/)).toBeInTheDocument();
  });

  it('filtro operou/nao operou vai para a API', async () => {
    const api = stubApi({
      'GET /settings': () => SETTINGS,
      'GET /decisions': () => page([makeDecision(1)]),
    });
    renderPage(<DecisoesPage />);
    await screen.findByTestId('resumo-dia');
    await userEvent.selectOptions(screen.getByLabelText('Mostrar'), 'traded');
    await waitFor(() =>
      expect(api.find('GET', '/decisions').at(-1)?.search).toContain('traded=true'),
    );
    await userEvent.selectOptions(screen.getByLabelText('Mostrar'), 'not-traded');
    await waitFor(() =>
      expect(api.find('GET', '/decisions').at(-1)?.search).toContain('traded=false'),
    );
  });

  it('pagina por cursor sem repetir itens', async () => {
    const api = stubApi({
      'GET /settings': () => SETTINGS,
      'GET /decisions': (_b, url) =>
        url.searchParams.get('cursor') === '2'
          ? page([makeDecision(2), makeDecision(1)])
          : page([makeDecision(3), makeDecision(2)], 2),
    });
    renderPage(<DecisoesPage />);
    await screen.findAllByRole('listitem');
    await userEvent.click(screen.getByRole('button', { name: 'Carregar decisões anteriores' }));
    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(3));
    expect(api.find('GET', '/decisions').at(-1)?.search).toContain('cursor=2');
    expect(screen.getByText(/chegou ao começo/)).toBeInTheDocument();
  });

  it('vazio, com filtro e sem filtro', async () => {
    stubApi({
      'GET /settings': () => SETTINGS,
      'GET /decisions': () => ({ items: [], next_cursor: null, summary: null }),
    });
    renderPage(<DecisoesPage />);
    expect(await screen.findByText('Ainda nenhuma decisão')).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Mostrar'), 'traded');
    expect(await screen.findByText('Nenhuma decisão com este filtro')).toBeInTheDocument();
  });

  it('erro mostra o card de tentar de novo', async () => {
    stubApi({ 'GET /settings': () => SETTINGS, 'GET /decisions': () => problem(500, 'Falha') });
    renderPage(<DecisoesPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Não foi possível carregar');
  });
});
