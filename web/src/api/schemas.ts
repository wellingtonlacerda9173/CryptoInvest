import { z } from 'zod';
import { BOT_STATES } from '@/lib/bot-state';
import type { components } from './generated/schema';

export type Schemas = components['schemas'];

/** Cada schema abaixo e conferido pelo compilador contra o tipo gerado do contrato (api/openapi.json). */
type Typed<K extends keyof Schemas> = z.ZodType<Schemas[K]>;

const str = z.string();
const num = z.number();
const nstr = z.string().nullable();
const nnum = z.number().nullable();

export const roleSchema = z.enum(['admin', 'reader']);
export type Role = z.infer<typeof roleSchema>;
export const ROLE_LABEL: Record<Role, string> = { admin: 'Administrador', reader: 'Leitor' };

export const userSchema: Typed<'UserOut'> = z.object({
  id: str,
  username: str,
  role: roleSchema,
  totp_enabled: z.boolean(),
  disabled: z.boolean(),
  created_at: str,
  locked_until: nstr.optional(),
});

export const meSchema: Typed<'MeOut'> = z.object({
  id: str,
  username: str,
  role: roleSchema,
  totp_enabled: z.boolean(),
  csrf_token: str,
  fresh_auth_until: nstr.optional(),
});
export type Me = Schemas['MeOut'];

export const loginStepSchema: Typed<'LoginStep'> = z.object({
  stage: z.enum(['totp', 'enrollment']),
  csrf_token: str,
  expires_in_seconds: num,
});

export const sessionStartedSchema: Typed<'SessionStarted'> = z.object({
  user: userSchema,
  csrf_token: str,
  expires_at: str,
  recovery_code_used: z.boolean().default(false),
});

export const enrollmentStartSchema: Typed<'EnrollmentStart'> = z.object({
  otpauth_uri: str,
  secret: str,
  qr_svg_data_uri: str,
});

export const enrollmentDoneSchema: Typed<'EnrollmentDone'> = z.object({
  user: userSchema,
  csrf_token: str,
  expires_at: str,
  recovery_codes: z.array(str),
});

export const recoveryCodesSchema: Typed<'RecoveryCodesOut'> = z.object({
  recovery_codes: z.array(str),
});

export const sessionRowSchema: Typed<'SessionOut'> = z.object({
  id: str,
  created_at: str,
  last_seen_at: str,
  expires_at: str,
  current: z.boolean(),
  ip: nstr,
  user_agent: nstr,
});
export const sessionListSchema = z.array(sessionRowSchema);
export type SessionRow = Schemas['SessionOut'];

const level = z.enum(['ok', 'warn', 'bad']);
export const riskLimitSchema: Typed<'RiskLimitOut'> = z.object({
  key: str,
  name: str,
  value: num,
  limit: num,
  unit: z.enum(['%', '']),
  level,
  summary: str,
  status_text: str,
  note: str,
  active_until: nstr.optional(),
});
export type RiskLimit = Schemas['RiskLimitOut'];
export const riskLimitsSchema = z.array(riskLimitSchema);

export const botStatusSchema: Typed<'BotStatusOut'> = z.object({
  run_id: nstr,
  mode: z.enum(['paper', 'live', 'backtest']),
  simulated: z.boolean(),
  state: z.enum(BOT_STATES),
  state_label: str,
  state_detail: str,
  connection: z.enum(['live', 'stale', 'offline', 'unknown']),
  heartbeat_at: nstr,
  heartbeat_age_seconds: nnum,
  started_at: nstr,
  code_version: nstr,
  stops_blind: z.boolean(),
  halted: z.boolean(),
  halt_reason: nstr,
  paused: z.boolean(),
  pause_reasons: z.array(z.object({ at: str, operator: str, reason: str })),
  outbox_pending: num,
  persistence_error: nstr,
  redis_healthy: z.boolean().nullable(),
  allow_open_calendar: z.boolean(),
  open_positions: z.array(str),
  pending_entries: z.array(str),
  equity: nnum,
  realized_equity: nnum,
  peak_equity: nnum,
  drawdown_pct: nnum,
  day_pnl: nnum,
  day_pnl_pct: nnum,
  daily_pause_until: nstr,
  target_reached_on: nstr,
  pair_pause_until: z.record(str, str),
  config_hash: nstr,
  config_version: nnum,
  limits: riskLimitsSchema,
  live: z.object({
    approved: z.boolean(),
    criteria_met: num,
    criteria_total: num,
    eligible: z.boolean(),
    live_available: z.boolean(),
    reason: nstr,
  }),
  pending_commands: num,
});
export type BotStatus = Schemas['BotStatusOut'];

export const accountSchema: Typed<'AccountSummaryOut'> = z.object({
  mode: z.enum(['paper', 'live', 'backtest']),
  simulated: z.boolean(),
  currency: str,
  initial_capital: nnum,
  equity: nnum,
  realized_equity: nnum,
  day_pnl: nnum,
  day_pnl_pct: nnum,
  week_pnl: nnum,
  week_pnl_pct: nnum,
  total_pnl: nnum,
  total_pnl_pct: nnum,
  as_of: nstr,
});
export type Account = Schemas['AccountSummaryOut'];

export const equitySchema: Typed<'EquitySeriesOut'> = z.object({
  mode: z.enum(['paper', 'live', 'backtest']),
  simulated: z.boolean(),
  initial_capital: nnum,
  points: z.array(z.object({ at: str, equity: num, realized: num })),
});
export type EquitySeries = Schemas['EquitySeriesOut'];

export type DecisionPage = Schemas['DecisionPage'];
export const decisionPageSchema: Typed<'DecisionPage'> = z.object({
  items: z.array(
    z.object({
      id: num,
      at: str,
      mode: z.enum(['paper', 'live', 'backtest']),
      run_id: str,
      symbol: str,
      action: z.enum(['enter', 'exit', 'hold', 'skip']),
      code: str,
      detail: str,
      outcome: str,
      outcome_kind: z.enum(['entered', 'waited', 'refused', 'info']),
      reason_text: str,
      strategy: nstr,
      confidence: nnum,
      traded: z.boolean(),
      signal: z.record(str, z.unknown()).nullable(),
    }),
  ),
  next_cursor: nnum,
  summary: z
    .object({ day: str, entries: num, refusals: num, signals: num, waits: num })
    .nullable()
    .optional(),
});
export type Decision = Schemas['DecisionOut'];

export const positionSchema: z.ZodType<Schemas['PositionOut']> = z.object({
  symbol: str,
  mode: z.enum(['paper', 'live', 'backtest']),
  run_id: str,
  opened_at: str,
  entry_price: num,
  initial_stop: num,
  stop: num,
  take_profit: nnum,
  quantity: num,
  risk_amount: num,
  last_price: nnum,
  unrealized_pnl: nnum,
  unrealized_r: nnum,
  breakeven_reached: z.boolean(),
  partial_done: z.boolean(),
  protected: z.boolean(),
  protection_note: str,
});
export type Position = Schemas['PositionOut'];
export const positionsSchema = z.array(positionSchema);

export const liveGateSchema: Typed<'LiveGateOut'> = z.object({
  run_id: nstr,
  mode: z.enum(['paper', 'live', 'backtest']),
  approved: z.boolean(),
  eligible_for_live: z.boolean(),
  verdict: str,
  ineligible_reason: nstr,
  criteria: z.array(
    z.object({
      code: str,
      name: str,
      status: z.enum(['met', 'failed', 'in_progress']),
      value: nnum,
      target: nnum,
      value_text: str,
      target_text: str,
      reason: nstr,
      missing_text: nstr,
    }),
  ),
  phrase_required: str,
  live_available: z.boolean(),
  unavailable_reason: str,
  first_activation_reduced_risk_days: num,
});
export type LiveGate = Schemas['LiveGateOut'];

export const COMMAND_NAMES = [
  'pause',
  'resume',
  'stop',
  'emergency-stop',
  'reactivate',
  'apply-config',
] as const;
export const COMMAND_STATES = [
  'pending_delivery',
  'delivered',
  'applied',
  'partially_applied',
  'refused',
  'discarded',
  'expired',
  'failed',
] as const;
const commandShape = {
  id: str,
  name: z.enum(COMMAND_NAMES),
  operator: str,
  reason: str,
  liquidate: z.boolean(),
  state: z.enum(COMMAND_STATES),
  state_text: str,
  detail: nstr.optional(),
  channel: z.enum(['redis', 'file']).nullable(),
  created_at: str,
  expires_at: nstr,
  delivered_at: nstr,
  resolved_at: nstr,
};
export const commandSchema: Typed<'CommandOut'> = z.object(commandShape);
export const commandAcceptedSchema: Typed<'CommandAccepted'> = z.object({
  ...commandShape,
  message: str,
});
export const commandPageSchema: Typed<'CommandPage'> = z.object({
  items: z.array(commandSchema),
  next_cursor: nstr.optional(),
});
export type Command = Schemas['CommandOut'];
export type CommandState = Command['state'];

export const powerSchema: Typed<'PowerOut'> = z.object({
  state: z.enum(['commanded', 'launched', 'waiting_runner']),
  message: str,
  command: commandAcceptedSchema.nullable().optional(),
});

export const onboardingSchema: Typed<'OnboardingOut'> = z.object({
  steps: z.array(
    z.object({
      key: z.enum(['admin', 'two_factor', 'binance_key', 'risk', 'paper']),
      title: str,
      detail: str,
      done: z.boolean(),
      required: z.boolean(),
    }),
  ),
  completed: z.boolean(),
  next_step: nstr,
  can_start_paper: z.boolean(),
  paper_running: z.boolean(),
  message: str,
});
export type Onboarding = Schemas['OnboardingOut'];

export const startPaperSchema: Typed<'StartPaperOut'> = z.object({
  state: z.enum(['started', 'already_running', 'resumed', 'waiting_runner', 'launched']),
  message: str,
  config_version: nnum,
  command_id: nstr.optional(),
});

const permissionsSchema: z.ZodType<Schemas['KeyPermissions']> = z.object({
  can_read: z.boolean(),
  can_trade: z.boolean(),
  can_transfer: z.boolean(),
  can_withdraw: z.boolean(),
  futures_enabled: z.boolean(),
  margin_enabled: z.boolean(),
  ip_restricted: z.boolean().nullable(),
});
export const credentialSchema: Typed<'CredentialOut'> = z.object({
  id: str,
  exchange: z.literal('binance'),
  label: str,
  api_key_last4: str,
  permissions: permissionsSchema,
  usable: z.boolean(),
  warnings: z.array(str).default([]),
  created_at: str,
  updated_at: str,
  last_verified_at: nstr,
  last_used_at: nstr,
});
export const credentialsSchema = z.array(credentialSchema);
export type Credential = Schemas['CredentialOut'];

export const settingsSchema = z.looseObject({
  can_edit: z.boolean(),
  capital_reference: num,
  version: z.looseObject({ id: num }).nullable(),
  document: z.looseObject({
    pairs: z.looseObject({ symbols: z.array(str), timeframe: str }),
    risk: z.looseObject({ risk_per_trade: str, max_daily_loss: str }),
  }),
});
export type SettingsRead = z.infer<typeof settingsSchema>;

export const settingsValidationSchema = z.looseObject({
  valid: z.boolean(),
  errors: z.array(z.object({ path: str, message: str })),
  increases_risk: z.boolean(),
  requires_acknowledgement: z.boolean(),
  activation_text: str,
});

export const problemTypeFreshAuth = 'fresh-auth-required';

export const stopAllRequestSchema = z.object({
  mode: z.enum(['block_new', 'liquidate']),
  operator: z.string().trim().max(64),
  reason: z.string().trim().max(280),
});
export type StopAllRequest = z.infer<typeof stopAllRequestSchema>;
