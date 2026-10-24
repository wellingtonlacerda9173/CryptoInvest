import type { FieldChange, FieldSpec, FullSettings } from '@/api/settingsSchemas';

const spec = (path: string, label: string, over: Partial<FieldSpec> = {}): FieldSpec => ({
  path,
  label,
  kind: 'number',
  default: null,
  minimum: null,
  maximum: null,
  hard_limit: false,
  unit: '',
  choices: null,
  ...over,
});

const risk = {
  risk_per_trade: '0.005',
  max_daily_loss: '0.01',
  max_open_positions: 2,
  max_drawdown: '0.05',
  allowed_assets: [],
  blocked_assets: ['BNB'],
  quote_asset: 'USDT',
  fee_asset: 'BNB',
};

const document = {
  pairs: { symbols: ['BTC/USDT', 'ETH/USDT'], timeframe: '1h' },
  strategies: {
    elephant_enabled: true,
    ema_enabled: true,
    min_confidence: 0.5,
    reward_risk: '2',
    elephant: { rsi_min: 50, detector: { lookback: 20 } },
    ema: { fast_period: 9 },
  },
  risk,
  ai: { enabled: false, model: 'claude-sonnet-5-5' },
};

export const makeFullSettings = (over: Partial<FullSettings> = {}): FullSettings => ({
  version: {
    id: 3,
    created_at: '2026-10-20T12:00:00Z',
    actor: 'ana.silva',
    reason: 'Configuração inicial',
    sections: ['risk'],
    activation: 'immediate',
    risk_increase: false,
    changes: [],
    application: 'none',
    application_text: '',
  },
  document,
  defaults: document,
  fields: {
    pairs: [
      spec('pairs.symbols', 'Pares', { kind: 'list' }),
      spec('pairs.timeframe', 'Tempo gráfico', {
        kind: 'choice',
        choices: ['15m', '1h', '4h'],
      }),
    ],
    strategies: [
      spec('strategies.elephant_enabled', 'Estratégia Barra elefante', { kind: 'boolean' }),
      spec('strategies.ema_enabled', 'Estratégia Cruzamento de EMAs', { kind: 'boolean' }),
      spec('strategies.min_confidence', 'Confiança mínima', {
        minimum: 0,
        maximum: 1,
        default: 0.5,
      }),
      spec('strategies.reward_risk', 'Retorno/risco alvo', {
        minimum: 2,
        maximum: 10,
        default: '2',
      }),
      spec('strategies.elephant.rsi_min', 'RSI mínimo', { minimum: 0, maximum: 100 }),
      spec('strategies.elephant.detector.lookback', 'Velas de comparação', {
        kind: 'integer',
        minimum: 2,
      }),
      spec('strategies.ema.fast_period', 'Média rápida', { kind: 'integer', minimum: 2 }),
    ],
    risk: [
      spec('risk.risk_per_trade', 'Risco por operação', {
        minimum: 0.0001,
        maximum: 0.01,
        hard_limit: true,
        unit: '%',
        default: '0.005',
      }),
      spec('risk.max_daily_loss', 'Perda diária máxima', {
        minimum: 0,
        maximum: 0.05,
        hard_limit: true,
        unit: '%',
        default: '0.01',
      }),
      spec('risk.max_open_positions', 'Posições abertas ao mesmo tempo', {
        kind: 'integer',
        minimum: 1,
        maximum: 2,
        hard_limit: true,
        default: 2,
      }),
      spec('risk.max_drawdown', 'Queda máxima do saldo (drawdown)', {
        minimum: 0,
        maximum: 0.05,
        hard_limit: true,
        unit: '%',
        default: '0.05',
      }),
      spec('risk.allowed_assets', 'Moedas permitidas', { kind: 'list', default: [] }),
      spec('risk.blocked_assets', 'Moedas bloqueadas', { kind: 'list', default: ['BNB'] }),
      spec('risk.quote_asset', 'Moeda de cotação', { kind: 'text' }),
    ],
    ai: [
      spec('ai.enabled', 'Assistente de IA', { kind: 'boolean', default: false }),
      spec('ai.model', 'Modelo de IA', { kind: 'text', default: 'claude-sonnet-5-5' }),
    ],
  },
  hard_limits: [
    {
      path: 'risk.risk_per_trade',
      label: 'Risco por operação',
      kind: 'max',
      value: 0.01,
      text: 'Risco por operação: no máximo 1,00%',
    },
    {
      path: 'risk.max_drawdown',
      label: 'Queda máxima do saldo',
      kind: 'max',
      value: 0.05,
      text: 'Queda máxima do saldo: no máximo 5,00%',
    },
  ],
  effective: {
    runner_connected: true,
    matches_latest: true,
    restart_rule: 'Aumentos só valem no próximo início.',
  },
  can_edit: true,
  capital_reference: 10000,
  ...over,
});

export const INCREASE_CHANGE: FieldChange = {
  path: 'risk.risk_per_trade',
  label: 'Risco por operação',
  before_text: '0,50%',
  after_text: '0,90%',
  effect: 'increase',
  increases_risk: true,
  cost_text: 'Cada operação perdida pode custar até 0,90% do saldo (cerca de 90,00 USDT).',
  applies: 'next_start',
};

export const DECREASE_CHANGE: FieldChange = {
  ...INCREASE_CHANGE,
  before_text: '0,50%',
  after_text: '0,25%',
  effect: 'decrease',
  increases_risk: false,
  cost_text: '',
  applies: 'now',
};

export const validation = (
  changes: FieldChange[],
  errors: { path: string; message: string }[] = [],
) => ({
  valid: errors.length === 0,
  errors,
  changes,
  increases_risk: changes.some((c) => c.increases_risk),
  requires_acknowledgement: changes.some((c) => c.increases_risk),
  activation: changes.some((c) => c.applies === 'next_start') ? 'next_start' : 'immediate',
  activation_text: changes.some((c) => c.applies === 'next_start')
    ? 'Só vale no próximo início do robô, sem nenhuma posição aberta.'
    : 'Vale agora: a mudança só reduz o risco.',
});

export const savedResult = (change: FieldChange, id = 4) => ({
  version: {
    id,
    created_at: '2026-10-24T12:00:00Z',
    actor: 'ana.silva',
    reason: 'Ajuste do risco',
    sections: ['risk'],
    activation: change.applies === 'now' ? 'immediate' : 'next_start',
    risk_increase: change.increases_risk,
    changes: [change],
    application: change.applies === 'now' ? 'applied' : 'none',
    application_text:
      change.applies === 'now'
        ? 'A redução de risco já está valendo no robô.'
        : 'Vale no próximo início do robô.',
  },
  activation: change.applies === 'now' ? 'immediate' : 'next_start',
  activation_text: change.applies === 'now' ? 'Vale agora.' : 'Só vale no próximo início do robô.',
  applied_now: [],
  pending_restart: [],
  command: null,
});
