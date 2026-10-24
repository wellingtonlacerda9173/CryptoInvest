export const PAIR_OPTIONS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT'] as const;
export const TIMEFRAMES = [
  ['15m', '15 minutos'],
  ['1h', '1 hora (recomendado)'],
  ['4h', '4 horas'],
  ['1d', '1 dia'],
] as const;
export const PRESETS = [
  ['0.25', 'Últimos 3 meses'],
  ['0.5', 'Últimos 6 meses'],
  ['1', 'Último ano'],
  ['2', 'Últimos 2 anos'],
  ['custom', 'Datas escolhidas por mim'],
] as const;
export const STRATEGY_OPTIONS = [
  ['elephant', 'Barra elefante', 'Procura uma vela muito maior que as vizinhas, com corpo forte.'],
  [
    'ema',
    'Cruzamento de médias (EMA)',
    'Entra quando a média rápida cruza a lenta a favor da tendência.',
  ],
] as const;
export const STEPS = ['Par', 'Período', 'Estratégias', 'Premissas', 'Revisar'] as const;

export const MIN_START = '2017-08-17';
export const MAX_SYMBOLS = 4;
const DAY_MS = 86_400_000;

export interface WizardState {
  symbols: string[];
  timeframe: string;
  preset: string;
  start: string;
  end: string;
  strategies: string[];
  minConfidence: string;
  rsiMin: string;
  rewardRisk: string;
  capital: string;
  takerFeePct: string;
  makerFeePct: string;
  slippageBps: string;
  latency: string;
  walkForward: boolean;
  trainDays: string;
  testDays: string;
  grid: string;
}

export const DEFAULT_WIZARD: WizardState = {
  symbols: ['BTC/USDT'],
  timeframe: '1h',
  preset: '1',
  start: '',
  end: '',
  strategies: ['elephant', 'ema'],
  minConfidence: '',
  rsiMin: '',
  rewardRisk: '',
  capital: '10000',
  takerFeePct: '0.10',
  makerFeePct: '0.10',
  slippageBps: '5',
  latency: '1',
  walkForward: false,
  trainDays: '180',
  testDays: '60',
  grid: '',
};

export type Errors = Partial<Record<keyof WizardState | 'period', string>>;

export const toNumber = (text: string): number =>
  text.trim() === '' ? NaN : Number(text.trim().replace(',', '.'));

const pairPattern = /^[A-Z0-9]{2,12}\/[A-Z0-9]{2,12}$/;
export const todayIso = (now = new Date()) => now.toISOString().slice(0, 10);

/** Dias do periodo escolhido (NaN enquanto as datas estiverem incompletas). */
export function periodDays(s: WizardState): number {
  if (s.preset !== 'custom') return Math.round(Number(s.preset) * 365);
  if (!s.start || !s.end) return NaN;
  return Math.floor((Date.parse(s.end) - Date.parse(s.start)) / DAY_MS);
}

function inRange(text: string, low: number, high: number, label: string, optional = false) {
  if (text.trim() === '') return optional ? undefined : 'Preencha este campo.';
  const n = toNumber(text);
  if (!Number.isFinite(n)) return 'Digite um número.';
  if (n < low || n > high) return `${label}: de ${low} a ${high}.`;
  return undefined;
}

export function validateStep(step: number, s: WizardState, today = todayIso()): Errors {
  const e: Errors = {};
  if (step === 0) {
    if (s.symbols.length === 0) e.symbols = 'Escolha pelo menos um par.';
    else if (s.symbols.length > MAX_SYMBOLS)
      e.symbols = `No máximo ${MAX_SYMBOLS} pares por teste.`;
    else if (!s.symbols.every((p) => pairPattern.test(p)))
      e.symbols = 'Use o formato MOEDA/MOEDA, por exemplo BTC/USDT.';
  }
  if (step === 1 && s.preset === 'custom') {
    if (!s.start) e.start = 'Escolha a data inicial.';
    else if (s.start < MIN_START) e.start = 'A Binance só tem dados a partir de 17/08/2017.';
    if (!s.end) e.end = 'Escolha a data final.';
    else if (s.end > today) e.end = 'A data final não pode estar no futuro.';
    if (!e.start && !e.end && s.start >= s.end)
      e.end = 'A data final precisa ser depois da inicial.';
    if (!e.start && !e.end && !e.period && periodDays(s) < 2)
      e.period = 'O período precisa ter pelo menos 2 dias.';
    if (!e.start && !e.end && periodDays(s) > 20 * 365) e.period = 'O período máximo é de 20 anos.';
  }
  if (step === 2 && s.strategies.length === 0) e.strategies = 'Escolha pelo menos uma estratégia.';
  if (step === 3) {
    e.minConfidence = inRange(s.minConfidence, 0, 1, 'Confiança', true);
    e.rsiMin = inRange(s.rsiMin, 0, 99, 'RSI mínimo', true);
    e.rewardRisk = inRange(s.rewardRisk, 2, 10, 'Retorno/risco', true);
    e.capital = inRange(s.capital, 0.01, 100_000_000, 'Capital');
    e.takerFeePct = inRange(s.takerFeePct, 0, 1, 'Taxa (%)');
    e.makerFeePct = inRange(s.makerFeePct, 0, 1, 'Taxa (%)');
    e.slippageBps = inRange(s.slippageBps, 0, 500, 'Deslizamento');
    e.latency = inRange(s.latency, 1, 10, 'Latência');
    if (!e.latency && !/^\d+$/.test(s.latency.trim())) e.latency = 'Use um número inteiro.';
    if (s.walkForward) {
      e.trainDays = inRange(s.trainDays, 7, 1500, 'Dias de treino');
      e.testDays = inRange(s.testDays, 1, 720, 'Dias de teste');
      const need = toNumber(s.trainDays) + toNumber(s.testDays);
      const have = periodDays(s);
      if (!e.trainDays && !e.testDays && Number.isFinite(have) && have < need)
        e.period = `O teste em janelas precisa de pelo menos ${need} dias (treino + teste) e o período tem ${have}.`;
      if (s.grid.length > 300) e.grid = 'A grade aceita até 300 caracteres.';
    }
  }
  for (const key of Object.keys(e) as (keyof Errors)[]) if (e[key] === undefined) delete e[key];
  return e;
}

export const validateAll = (s: WizardState, today = todayIso()): Errors =>
  Object.assign({}, ...STEPS.map((_, i) => validateStep(i, s, today))) as Errors;

/** Primeiro passo com erro (-1 se tudo estiver certo). */
export const firstInvalidStep = (s: WizardState, today = todayIso()): number =>
  STEPS.findIndex((_, step) => Object.keys(validateStep(step, s, today)).length > 0);

const round = (n: number, digits: number) => Number(n.toFixed(digits));

export function buildRequest(s: WizardState): Record<string, unknown> {
  const params: Record<string, number> = {};
  if (s.minConfidence.trim()) params.min_confidence = toNumber(s.minConfidence);
  if (s.rsiMin.trim()) params.rsi_min = toNumber(s.rsiMin);
  if (s.rewardRisk.trim()) params.reward_risk = toNumber(s.rewardRisk);
  return {
    symbols: s.symbols,
    timeframe: s.timeframe,
    ...(s.preset === 'custom'
      ? { start: `${s.start}T00:00:00Z`, end: `${s.end}T00:00:00Z` }
      : { years: Number(s.preset) }),
    strategies: s.strategies,
    params,
    capital: toNumber(s.capital),
    taker_fee: round(toNumber(s.takerFeePct) / 100, 6),
    maker_fee: round(toNumber(s.makerFeePct) / 100, 6),
    slippage_bps: toNumber(s.slippageBps),
    latency_candles: toNumber(s.latency),
    walk_forward: s.walkForward,
    train_days: toNumber(s.trainDays),
    test_days: toNumber(s.testDays),
    ...(s.walkForward && s.grid.trim() ? { grid: s.grid.trim() } : {}),
  };
}

const pretty = (iso: string) => iso.split('-').reverse().join('/');

export function describePeriod(s: WizardState): string {
  if (s.preset === 'custom')
    return s.start && s.end
      ? `de ${pretty(s.start)} a ${pretty(s.end)}`
      : 'datas ainda não escolhidas';
  return (PRESETS.find(([v]) => v === s.preset)?.[1] ?? '').toLowerCase();
}

const text = (v: unknown, fallback: string) =>
  typeof v === 'string' || typeof v === 'number' ? String(v) : fallback;
const list = (v: unknown, fallback: string[]) =>
  Array.isArray(v) ? v.filter((i): i is string => typeof i === 'string') : fallback;
const fromFraction = (v: unknown, fallback: string) =>
  typeof v === 'string' || typeof v === 'number'
    ? String(Number((Number(v) * 100).toFixed(4)))
    : fallback;

/** Premissas de um backtest anterior (`job.params`, ja resolvidas) de volta ao assistente. */
export function stateFromParams(p: Record<string, unknown>): WizardState {
  const d = DEFAULT_WIZARD;
  const params = (p.params ?? {}) as Record<string, unknown>;
  return {
    symbols: list(p.symbols, d.symbols),
    timeframe: text(p.timeframe, d.timeframe),
    preset: 'custom',
    start: text(p.start, '').slice(0, 10),
    end: text(p.end, '').slice(0, 10),
    strategies: list(p.strategies, d.strategies),
    minConfidence: text(params.min_confidence, ''),
    rsiMin: text(params.rsi_min, ''),
    rewardRisk: text(params.reward_risk, ''),
    capital: text(p.capital, d.capital),
    takerFeePct: fromFraction(p.taker_fee, d.takerFeePct),
    makerFeePct: fromFraction(p.maker_fee, d.makerFeePct),
    slippageBps: text(p.slippage_bps, d.slippageBps),
    latency: text(p.latency_candles, d.latency),
    walkForward: p.walk_forward === true,
    trainDays: text(p.train_days, d.trainDays),
    testDays: text(p.test_days, d.testDays),
    grid: text(p.grid, ''),
  };
}
