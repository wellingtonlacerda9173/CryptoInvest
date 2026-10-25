import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { AiUsage, AiValue, Lesson, LessonDetail } from '@/api/aiSchemas';
import { FRESH_REQUIRED, problem, renderPage, stubApi } from '@/test/render';
import { AiUsagePanel } from './AiUsagePanel';
import { AiValueTab } from './AiValueTab';
import { LessonsTab } from './LessonsTab';

const ID = 'a'.repeat(32);

const lesson = (over: Partial<Lesson> = {}): Lesson => ({
  id: ID,
  created_at: '2026-10-24T12:00:00Z',
  period_start: '2026-10-17T12:00:00Z',
  period_end: '2026-10-24T12:00:00Z',
  source: 'ai',
  model: 'claude-haiku-5-5',
  prompt_version: 'lessons-v1',
  status: 'pending',
  reviewer: null,
  review_reason: null,
  reviewed_at: null,
  summary: 'Semana mista com perdas em lateral.',
  observations: [
    {
      text: 'A <b>elephant</b> perdeu mais em lateral.',
      evidence: '4 perdas em 6 trades',
      confidence: 'low',
    },
  ],
  ...over,
});

const detail = (over: Partial<LessonDetail> = {}): LessonDetail => ({
  ...lesson(),
  stats: {
    trades: 6,
    wins: 2,
    losses: 4,
    total_r: -1.5,
    loss_r: -4,
    skipped: 9,
    by_strategy: [
      {
        key: 'elephant',
        trades: 6,
        wins: 2,
        losses: 4,
        total_r: -1.5,
        mean_r: -0.25,
        loss_r: -4,
        worst_r: -1,
        max_drawdown_r: 3,
      },
    ],
    by_regime: [],
    by_symbol: [],
    worst_losses: [
      {
        strategy: 'elephant',
        symbol: 'BTC/USDT',
        regime: 'lateral',
        closed_on: '2026-10-20',
        r_multiple: -1,
      },
    ],
    decisions: { ai_veto: 2 },
  },
  ...over,
});

const value = (over: Partial<AiValue> = {}): AiValue => ({
  period_start: '2026-09-24T00:00:00Z',
  period_end: '2026-10-24T00:00:00Z',
  calls: 12,
  failed_calls: 1,
  cost_usd: 0.31,
  vetoes: 4,
  simulated: 3,
  not_filled: 1,
  unresolved: 0,
  not_simulated: 0,
  avoided_loss: 120,
  missed_gain: 40,
  net_avoided: 80,
  correct_vetoes: 2,
  false_vetoes: 1,
  false_veto_rate: 0.3333,
  mean_r_vetoed: -0.4,
  approved_trades: 5,
  approved_wins: 2,
  approved_losses: 3,
  approved_pnl: -35,
  approved_loss_pnl: -150,
  approved_total_r: -0.6,
  net_value_usd: 79.69,
  verdict: 'insufficient_sample',
  verdict_text: 'Amostra insuficiente: só 3 vetos simulados (mínimo 10).',
  small_sample: true,
  cases: [
    {
      decided_at: '2026-10-20T10:00:00Z',
      code: 'ai_veto',
      strategy: 'elephant',
      symbol: 'BTC/USDT',
      status: 'stop',
      pnl: -60,
      r_multiple: -1,
    },
  ],
  ...over,
});

const usage = (over: Partial<AiUsage> = {}): AiUsage => ({
  month_start: '2026-10-01T00:00:00Z',
  spent_usd: 4.5,
  budget_usd: 20,
  calls: 30,
  failures: 2,
  budget_stops: 1,
  by_result: { ok: 27 },
  runner_status: 'ligada',
  circuit_open: false,
  calls_last_hour: 3,
  ...over,
});

describe('Uso da IA', () => {
  it('mostra gasto contra o orcamento, circuito e a regra de que a IA so veta', async () => {
    stubApi({ 'GET /ai/usage': () => usage({ circuit_open: true }) });
    renderPage(<AiUsagePanel />);
    expect(await screen.findByText(/A IA só veta ou reduz a confiança/)).toBeInTheDocument();
    expect(
      await screen.findByRole('group', { name: /US\$ 4,50 de US\$ 20,00/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Circuito aberto/)).toBeInTheDocument();
    expect(screen.getByText('Robô: IA ligada')).toBeInTheDocument();
  });

  it('erro de dados fica no painel com opcao de tentar de novo', async () => {
    stubApi({ 'GET /ai/usage': () => problem(500, 'Erro') });
    renderPage(<AiUsagePanel />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/Não foi possível carregar/);
    expect(screen.getByRole('button', { name: /Tentar de novo/ })).toBeInTheDocument();
  });
});

describe('Licoes da IA', () => {
  it('vazio explica quando saem as licoes', async () => {
    stubApi({ 'GET /ai/lessons': () => ({ items: [], next_cursor: null }) });
    renderPage(<LessonsTab />);
    expect(await screen.findByText('Ainda não há lições')).toBeInTheDocument();
  });

  it('exibe o texto da IA como TEXTO (sem HTML), com evidencia numerica e aviso de contexto de leitura', async () => {
    stubApi({
      'GET /ai/lessons': () => ({ items: [lesson()], next_cursor: null }),
      [`GET /ai/lessons/${ID}`]: () => detail(),
    });
    const { container } = renderPage(<LessonsTab />);
    expect(await screen.findByText(/A <b>elephant<\/b> perdeu/)).toBeInTheDocument();
    expect(container.querySelector('b')).toBeNull();
    expect(screen.getByText(/Evidência: 4 perdas em 6 trades/)).toBeInTheDocument();
    expect(screen.getByText(/nunca mudam/)).toHaveTextContent(/contexto de leitura/);
    expect(screen.getByText(/4 perdas/, { selector: 'strong' })).toBeInTheDocument();
    expect(screen.getAllByText('Pendente de revisão')).toHaveLength(2);
  });

  it('leitor so le: botoes desabilitados com explicacao e nenhuma chamada de escrita', async () => {
    const api = stubApi({
      'GET /ai/lessons': () => ({ items: [lesson()], next_cursor: null }),
      [`GET /ai/lessons/${ID}`]: () => detail(),
    });
    renderPage(<LessonsTab />, { role: 'reader' });
    const approve = await screen.findByRole('button', { name: 'Aprovar' });
    expect(approve).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Rejeitar' })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Gerar agora/ })).toBeDisabled();
    expect(screen.getAllByText(/Só administradores/).length).toBeGreaterThan(0);
    expect(api.calls.filter((c) => c.method === 'POST')).toHaveLength(0);
  });

  it('admin aprova com motivo obrigatorio e reautenticacao quando o servidor pede', async () => {
    let attempts = 0;
    const api = stubApi({
      'GET /ai/lessons': () => ({ items: [lesson()], next_cursor: null }),
      [`GET /ai/lessons/${ID}`]: () => detail(),
      [`POST /ai/lessons/${ID}/review`]: () => {
        attempts += 1;
        return attempts === 1 ? FRESH_REQUIRED() : detail({ status: 'approved' });
      },
    });
    renderPage(<LessonsTab />);
    await userEvent.click(await screen.findByRole('button', { name: 'Aprovar' }));
    const dialog = await screen.findByRole('alertdialog');
    const confirm = within(dialog).getByRole('button', { name: 'Aprovar' });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/Motivo/), 'bate com o que vi');
    await userEvent.click(confirm);
    const code = await screen.findByLabelText(/código/i);
    await userEvent.type(code, '123456');
    await userEvent.click(screen.getByRole('button', { name: /Confirmar/ }));
    await waitFor(() => expect(api.find('POST', `/ai/lessons/${ID}/review`)).toHaveLength(2));
    expect(api.find('POST', `/ai/lessons/${ID}/review`)[1]?.body).toEqual({
      decision: 'approve',
      reason: 'bate com o que vi',
    });
  });

  it('erro de rede na lista mostra tentar de novo', async () => {
    stubApi({ 'GET /ai/lessons': () => problem(500, 'Erro') });
    renderPage(<LessonsTab />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});

describe('Valor da IA', () => {
  it('sinaliza amostra pequena, mostra perdas aprovadas e deixa claro que e simulacao', async () => {
    stubApi({ 'GET /ai/value': () => value() });
    renderPage(<AiValueTab />);
    expect(await screen.findByText(/Amostra insuficiente/)).toBeInTheDocument();
    expect(screen.getByText(/não tire conclusões ainda/)).toBeInTheDocument();
    expect(screen.getByText('3 perdas', { selector: 'strong' })).toBeInTheDocument();
    expect(screen.getByText(/estimativas de uma simulação pessimista/)).toBeInTheDocument();
    expect(screen.getByText('Falsos vetos')).toBeInTheDocument();
  });

  it('sem atividade da IA mostra o vazio explicativo', async () => {
    stubApi({
      'GET /ai/value': () =>
        value({
          verdict: 'no_activity',
          verdict_text: 'Sem atividade da IA no período.',
          calls: 0,
          vetoes: 0,
          cases: [],
        }),
    });
    renderPage(<AiValueTab />);
    expect(await screen.findByText('Sem atividade da IA neste período')).toBeInTheDocument();
  });

  it('trocar o periodo consulta a API com os dias escolhidos e erro mostra tentar de novo', async () => {
    const api = stubApi({ 'GET /ai/value': () => value() });
    renderPage(<AiValueTab />);
    await screen.findByText(/Amostra insuficiente/);
    await userEvent.selectOptions(screen.getByLabelText('Período'), '90');
    await waitFor(() => expect(api.find('GET', '/ai/value').at(-1)?.search).toContain('days=90'));
  });

  it('erro de dados fica no card', async () => {
    stubApi({ 'GET /ai/value': () => problem(500, 'Erro') });
    renderPage(<AiValueTab />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
