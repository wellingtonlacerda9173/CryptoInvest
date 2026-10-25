import type { Candles, GroupStats, Performance, Trade, TradePage } from '@/api/marketSchemas';
import type { Decision } from '@/api/schemas';

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString();

export const SETTINGS = {
  can_edit: true,
  capital_reference: 10000,
  version: { id: 1 },
  document: {
    pairs: { symbols: ['BTC/USDT', 'ETH/USDT'], timeframe: '1h' },
    risk: { risk_per_trade: '0.005', max_daily_loss: '0.02' },
  },
};

export const makeCandles = (patch: Partial<Candles> = {}): Candles => ({
  mode: 'paper',
  simulated: true,
  symbol: 'BTC/USDT',
  timeframe: '1h',
  candles: Array.from({ length: 30 }, (_, i) => ({
    t: ago((30 - i) * 60),
    o: 100 + i,
    h: 102 + i,
    l: 99 + i,
    c: 101 + i,
    v: 1000,
  })),
  markers: [{ t: ago(300), kind: 'elephant', price: 120, text: 'Barra elefante' }],
  levels: [
    { kind: 'stop', price: 95, since: ago(200) },
    { kind: 'entry', price: 100, since: ago(200) },
    { kind: 'target', price: 110, since: ago(200) },
  ],
  last_price: 130.5,
  change_24h_pct: 1.25,
  volume_24h: 24000,
  last_decision: 'Esperou: sem sinal.',
  blind_since: null,
  decisions: [],
  decisions_truncated: false,
  ...patch,
});

export const makeDecision = (id: number, patch: Partial<Decision> = {}): Decision => ({
  id,
  at: ago(id * 60),
  mode: 'paper',
  run_id: 'paper-1',
  symbol: 'BTC/USDT',
  action: 'skip',
  code: 'no_signal',
  detail: 'no_signal: mensagem tecnica',
  outcome: 'Esperou',
  outcome_kind: 'waited',
  reason_text: 'Sem sinal agora: o mercado está lateral.',
  strategy: null,
  confidence: null,
  traded: false,
  signal: null,
  ...patch,
});

export const makeTrade = (id: number, patch: Partial<Trade> = {}): Trade => ({
  id,
  trade_id: `t${id}`,
  run_id: 'paper-1',
  mode: 'paper',
  symbol: 'BTC/USDT',
  strategy: 'elephant',
  opened_at: ago(id * 600 + 300),
  closed_at: ago(id * 600),
  entry_price: 110,
  exit_price: 112,
  quantity: 1,
  fees: 0.2,
  pnl: 12,
  r_multiple: 2,
  exit_reason: 'take_profit',
  exit_reason_text: 'Alvo atingido',
  result: 'win',
  ...patch,
});

export const LOSS = (id: number) =>
  makeTrade(id, {
    pnl: -50,
    r_multiple: -1,
    exit_price: 105,
    exit_reason: 'stop_loss',
    exit_reason_text: 'Stop acionado',
    result: 'loss',
  });

export const makeTradePage = (items: Trade[], next: number | null = null): TradePage => ({
  items,
  next_cursor: next,
  summary: {
    count: items.length,
    wins: items.filter((t) => t.result === 'win').length,
    losses: items.filter((t) => t.result === 'loss').length,
    pnl: items.reduce((s, t) => s + t.pnl, 0),
    fees: items.length * 0.2,
  },
});

const group = (key: string, trades: number, pnl: number): GroupStats => ({
  key,
  trades,
  wins: Math.ceil(trades / 2),
  pnl,
  win_rate: 0.5,
  profit_factor: 1.4,
});

export const makePerformance = (patch: Partial<Performance> = {}): Performance => ({
  mode: 'paper',
  simulated: true,
  run_id: 'paper-1',
  period_start: ago(60 * 24 * 10),
  period_end: ago(0),
  trades: 9,
  wins: 5,
  losses: 3,
  signals_refused: 12,
  total_pnl: 150,
  total_return_pct: 1.5,
  max_drawdown_pct: 2.3,
  profit_factor: 1.8,
  expectancy_r: 0.4,
  win_rate: 0.5556,
  payoff: 1.6,
  sharpe: 0.9,
  idle_pct: 76,
  months: 1,
  positive_months: 1,
  by_month: [group('2026-10', 9, 150)],
  by_pair: [group('BTC/USDT', 6, 120), group('ETH/USDT', 3, -30)],
  by_strategy: [group('elephant', 6, 180), group('ema', 3, -30)],
  avoided: [{ code: 'event_window', count: 4, label: 'Perto de notícia importante' }],
  notes: ['Resultado de paper trading, com preços reais e dinheiro simulado.'],
  sample_warning: '9 operações: poucas para concluir. Trate como indício, não como prova.',
  ...patch,
});
