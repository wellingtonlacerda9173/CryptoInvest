import type {
  Account,
  BotStatus,
  Command,
  Credential,
  Decision,
  EquitySeries,
  LiveGate,
  Onboarding,
  Position,
  RiskLimit,
  SessionRow,
} from '@/api/schemas';

// Dados FICTICIOS (MSW em VITE_MOCK=1 e testes). Nada daqui vai para o bundle de producao.
export const MOCK_USERS = {
  admin: {
    id: '00000000-0000-4000-8000-000000000001',
    username: 'ana.silva',
    role: 'admin' as const,
  },
  reader: {
    id: '00000000-0000-4000-8000-000000000002',
    username: 'leitor.exemplo',
    role: 'reader' as const,
  },
};
export const MOCK_PASSWORD = 'senha-de-teste-123';
export const MOCK_TOTP = '123456';
export const MOCK_CSRF = 'mock-csrf-token';
export const MOCK_ACTIVATION = 'ativacao-de-teste';
export const MOCK_RECOVERY = ['AAAA-1111', 'BBBB-2222', 'CCCC-3333', 'DDDD-4444'];

const now = () => new Date().toISOString();
const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();

export const makeLimits = (): RiskLimit[] => [
  {
    key: 'daily_loss',
    name: 'Perda diária',
    value: 0.3,
    limit: 1,
    unit: '%',
    level: 'ok',
    status_text: 'Folgado',
    summary: 'Perda diária: 0,3% de 1% - Folgado',
    note: 'se chegar a 1%, o robô para de abrir operações até 00:00 UTC',
  },
  {
    key: 'drawdown',
    name: 'Drawdown',
    value: 1.8,
    limit: 5,
    unit: '%',
    level: 'ok',
    status_text: 'Folgado',
    summary: 'Drawdown: 1,8% de 5% - Folgado',
    note: 'se chegar a 5%, o robô desliga e só você religa',
  },
  {
    key: 'loss_streak',
    name: 'Perdas seguidas (ETH/USDT)',
    value: 1,
    limit: 2,
    unit: '',
    level: 'warn',
    status_text: 'Atenção: 1 perda a menos do limite',
    summary: 'Perdas seguidas (ETH/USDT): 1 de 2 - Atenção',
    note: 'com 2 seguidas o par pausa por 24 h',
  },
  {
    key: 'open_positions',
    name: 'Posições abertas',
    value: 1,
    limit: 2,
    unit: '',
    level: 'ok',
    status_text: 'Normal',
    summary: 'Posições abertas: 1 de 2',
    note: 'máximo de 2 ao mesmo tempo',
  },
  {
    key: 'total_exposure',
    name: 'Exposição total',
    value: 24,
    limit: 50,
    unit: '%',
    level: 'ok',
    status_text: 'Normal',
    summary: 'Exposição total: 24% de 50%',
    note: 'parte do saldo que está comprada',
  },
];

export const makeStatus = (patch: Partial<BotStatus> = {}): BotStatus => ({
  run_id: 'paper-20261018-091200',
  mode: 'paper',
  simulated: true,
  state: 'RUNNING',
  state_label: 'Operando',
  state_detail: 'Procurando oportunidades.',
  connection: 'live',
  heartbeat_at: now(),
  heartbeat_age_seconds: 12,
  started_at: ago(60 * 24 * 5),
  code_version: 'teste',
  stops_blind: false,
  halted: false,
  halt_reason: null,
  paused: false,
  pause_reasons: [],
  outbox_pending: 0,
  persistence_error: null,
  redis_healthy: true,
  allow_open_calendar: true,
  open_positions: ['BTC/USDT'],
  pending_entries: [],
  equity: 10184.32,
  realized_equity: 10153.72,
  peak_equity: 10371.05,
  drawdown_pct: 1.8,
  day_pnl: -30.56,
  day_pnl_pct: -0.3,
  daily_pause_until: null,
  target_reached_on: null,
  pair_pause_until: {},
  config_hash: 'abc',
  config_version: 1,
  limits: makeLimits(),
  live: {
    approved: false,
    criteria_met: 2,
    criteria_total: 6,
    eligible: false,
    live_available: false,
    reason: null,
  },
  pending_commands: 0,
  ...patch,
});

export const makeAccount = (patch: Partial<Account> = {}): Account => ({
  mode: 'paper',
  simulated: true,
  currency: 'USDT',
  initial_capital: 10000,
  equity: 10184.32,
  realized_equity: 10153.72,
  day_pnl: -30.56,
  day_pnl_pct: -0.3,
  week_pnl: 80,
  week_pnl_pct: 0.8,
  total_pnl: 184.32,
  total_pnl_pct: 1.84,
  as_of: now(),
  ...patch,
});

export const makeEquity = (): EquitySeries => ({
  mode: 'paper',
  simulated: true,
  initial_capital: 10000,
  points: Array.from({ length: 30 }, (_, i) => ({
    at: ago((29 - i) * 60 * 24),
    equity: 10000 + i * 6 + Math.sin(i / 2) * 40,
    realized: 10000 + i * 6,
  })),
});

export const makeDecisions = (): { items: Decision[]; next_cursor: null } => ({
  items: [
    [
      'BTC/USDT',
      'enter',
      'entered',
      'Comprou',
      'Barra elefante com rompimento. Risco 0,5%, alvo 2 R.',
      '',
    ],
    [
      'ETH/USDT',
      'skip',
      'refused',
      'Não entrou',
      'Há um evento de mercado nos próximos 30 min.',
      'event_window',
    ],
    ['BTC/USDT', 'hold', 'waited', 'Esperou', 'Sem sinal: mercado lateral. Ficar em USDT.', ''],
    [
      'ETH/USDT',
      'skip',
      'refused',
      'Não entrou',
      'Retorno/risco 1:1,4; o mínimo é 1:2.',
      'reward_risk',
    ],
  ].map(([symbol, action, kind, outcome, reason, code], i) => ({
    id: 100 - i,
    at: ago(60 * (i + 1)),
    mode: 'paper' as const,
    run_id: 'paper-20261018-091200',
    symbol: symbol as string,
    action: action as Decision['action'],
    code: code as string,
    detail: '',
    outcome: outcome as string,
    outcome_kind: kind as Decision['outcome_kind'],
    reason_text: reason as string,
    strategy: 'elephant',
    confidence: 0.7,
    traded: kind === 'entered',
    signal: null,
  })),
  next_cursor: null,
});

export const makePositions = (): Position[] => [
  {
    symbol: 'BTC/USDT',
    mode: 'paper',
    run_id: 'paper-20261018-091200',
    opened_at: ago(120),
    entry_price: 67420.5,
    initial_stop: 66980,
    stop: 66980,
    take_profit: 68301.5,
    quantity: 0.0231,
    risk_amount: 50,
    last_price: 67560,
    unrealized_pnl: 30.6,
    unrealized_r: 0.6,
    breakeven_reached: false,
    partial_done: false,
    protected: true,
    protection_note: 'Stop ativo',
  },
];

export const makeGate = (): LiveGate => ({
  run_id: 'paper-20261018-091200',
  mode: 'paper',
  approved: false,
  eligible_for_live: false,
  verdict: 'Ainda não.',
  ineligible_reason: null,
  criteria: [
    ['days', 'Pelo menos 60 dias de teste no Paper', 'in_progress', '23', '60'],
    ['drawdown', 'Drawdown máximo abaixo de 5%', 'met', '1,8%', '5%'],
    ['profit_factor', 'Profit factor acima de 1,5', 'failed', '1,34', '1,5'],
    ['trades', 'Pelo menos 30 operações', 'in_progress', '17', '30'],
    ['losses', 'Pelo menos 5 perdas na amostra', 'met', '6', '5'],
    [
      'eligible_run',
      'Run elegível (calendário de eventos ligado o tempo todo)',
      'failed',
      'Inelegível',
      'Elegível',
    ],
  ].map(([code, name, status, value_text, target_text]) => ({
    code: code as string,
    name: name as string,
    status: status as LiveGate['criteria'][number]['status'],
    value: null,
    target: null,
    value_text: value_text as string,
    target_text: target_text as string,
    reason: null,
    missing_text: null,
  })),
  phrase_required: 'OPERAR COM DINHEIRO REAL',
  live_available: false,
  unavailable_reason: 'O Live será liberado em uma fase futura.',
  first_activation_reduced_risk_days: 1,
});

export const makeOnboarding = (completed = false): Onboarding => ({
  steps: [
    {
      key: 'admin',
      title: 'Conta de administrador',
      detail: 'Pronto.',
      done: true,
      required: true,
    },
    {
      key: 'two_factor',
      title: 'Aplicativo autenticador',
      detail: 'Pronto.',
      done: true,
      required: true,
    },
    {
      key: 'binance_key',
      title: 'Chave da Binance',
      detail: 'Opcional no Paper.',
      done: false,
      required: false,
    },
    { key: 'risk', title: 'Risco', detail: 'Revise os limites.', done: completed, required: true },
    {
      key: 'paper',
      title: 'Iniciar no Paper',
      detail: 'Falta iniciar.',
      done: completed,
      required: true,
    },
  ],
  completed,
  next_step: completed ? null : 'risk',
  can_start_paper: true,
  paper_running: completed,
  message: 'Falta configurar o risco e iniciar no Paper.',
});

export const makeCredential = (): Credential => ({
  id: '00000000-0000-4000-8000-0000000000c1',
  exchange: 'binance',
  label: 'Principal',
  api_key_last4: 'a91f',
  permissions: {
    can_read: true,
    can_trade: true,
    can_transfer: false,
    can_withdraw: false,
    futures_enabled: false,
    margin_enabled: false,
    ip_restricted: true,
  },
  usable: true,
  warnings: [],
  created_at: now(),
  updated_at: now(),
  last_verified_at: now(),
  last_used_at: null,
});

export const makeSessions = (): SessionRow[] => [
  {
    id: '00000000-0000-4000-8000-0000000000a1',
    created_at: ago(30),
    last_seen_at: now(),
    expires_at: ago(-600),
    current: true,
    ip: '127.0.0.1',
    user_agent: 'Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36',
  },
  {
    id: '00000000-0000-4000-8000-0000000000a2',
    created_at: ago(600),
    last_seen_at: ago(240),
    expires_at: ago(-60),
    current: false,
    ip: '192.0.2.10',
    user_agent: 'Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile Safari/537.36',
  },
];

export const makeCommand = (patch: Partial<Command> = {}): Command => ({
  id: 'cmd-1',
  name: 'pause',
  operator: 'ana.silva',
  reason: 'teste',
  liquidate: false,
  state: 'applied',
  state_text: 'Aplicado',
  detail: null,
  channel: 'redis',
  created_at: ago(5),
  expires_at: null,
  delivered_at: ago(5),
  resolved_at: ago(4),
  ...patch,
});

// Estado mutavel do modo VITE_MOCK=1 (os testes usam os makers diretamente).
let runtime: BotStatus = makeStatus();
export const getMockStatus = () => runtime;
export const patchMockStatus = (patch: Partial<BotStatus>) => {
  runtime = { ...runtime, ...patch };
};
export const resetMockStatus = () => {
  runtime = makeStatus();
};
