import { z } from 'zod';

const str = z.string();
const num = z.number();
const nstr = z.string().nullable();
const nnum = z.number().nullable();
/** Decimais chegam como texto ("12.34"); o painel so exibe, entao viram numero ao ler. */
const decimal = z.union([z.number(), z.string()]).transform(Number);

export const JOB_STATUSES = ['queued', 'running', 'succeeded', 'failed', 'cancelled'] as const;
export const jobStatusSchema = z.enum(JOB_STATUSES);
export type JobStatus = z.infer<typeof jobStatusSchema>;
export const ACTIVE_STATUSES: readonly JobStatus[] = ['queued', 'running'];

export const backtestJobSchema = z.looseObject({
  id: str,
  status: jobStatusSchema,
  created_by: str,
  created_at: str,
  started_at: nstr,
  finished_at: nstr,
  cancel_requested: z.boolean(),
  params: z.record(z.string(), z.unknown()),
  progress: z.looseObject({
    stage: str,
    percent: num,
    message: str,
    symbol: nstr.optional(),
  }),
  error: nstr,
  has_report: z.boolean(),
});
export type BacktestJob = z.infer<typeof backtestJobSchema>;

export const backtestPageSchema = z.object({
  items: z.array(backtestJobSchema),
  next_cursor: nstr.optional(),
});

const monthly = z.object({ month: str, return_pct: num, realized_pnl: decimal, trades: num });
const group = z.object({
  name: str,
  trades: num,
  win_rate: nnum,
  pnl: decimal,
  profit_factor: nnum,
  expectancy_r: nnum,
});

export const metricsSchema = z.looseObject({
  max_drawdown: num,
  max_drawdown_days: num,
  positive_months_ratio: nnum,
  worst_month_return: nnum,
  total_return: num,
  total_pnl: decimal,
  final_equity: decimal,
  trades: num,
  wins: num,
  losses: num,
  win_rate: nnum,
  profit_factor: nnum,
  payoff: nnum,
  expectancy_r: nnum,
  sharpe: nnum,
  idle_pct: num,
  fees_paid: decimal,
  monthly: z.array(monthly),
  by_strategy: z.array(group),
});
export type Metrics = z.infer<typeof metricsSchema>;

const curve = z.array(z.object({ at: str, equity: num }));

const avoidedGroup = z.object({
  code: str,
  kind: str,
  signals: num,
  simulated: num,
  hypothetical_pnl: decimal,
  avoided_losses: decimal,
  forgone_gains: decimal,
});

export const avoidedSchema = z.looseObject({
  signals: num,
  simulated: num,
  avoided_losses: decimal,
  forgone_gains: decimal,
  net_effect: decimal,
  capacity_net_effect: decimal,
  state_locks_net_effect: decimal,
  risk_vetoes_net_effect: decimal,
  halt_at: nstr,
  by_code: z.array(avoidedGroup),
});
export type Avoided = z.infer<typeof avoidedSchema>;
export type AvoidedGroup = z.infer<typeof avoidedGroup>;

const tripSchema = z.object({
  strategy: str,
  opened_at: str,
  closed_at: str,
  entry_price: decimal,
  quantity: decimal,
  pnl: decimal,
  fees: decimal,
  r_multiple: decimal,
  exits: z.array(str),
});
export type ReportTrade = z.infer<typeof tripSchema>;

export const walkForwardSchema = z.union([
  z.object({ error: str }),
  z.looseObject({
    windows: z.array(
      z.looseObject({
        index: num,
        test_start: str,
        test_end: str,
        selected: z.record(z.string(), z.string()).nullable(),
        train_return: nnum,
        test_return: nnum,
        test_trades: nnum,
      }),
    ),
    out_of_sample: metricsSchema,
    out_of_sample_curve: curve,
    assessment: z.looseObject({
      verdict: str,
      flags: z.array(str),
      in_sample_return_per_30d: nnum,
      out_of_sample_return_per_30d: nnum,
    }),
  }),
]);
export type WalkForward = z.infer<typeof walkForwardSchema>;

export const symbolReportSchema = z.looseObject({
  symbol: str,
  timeframe: str,
  data_source: str,
  data_notes: z.array(str),
  candles: num,
  period: z.object({ start: str, end: str }),
  metrics: metricsSchema,
  equity_curve: curve,
  trades: z.array(tripSchema),
  trades_total: num,
  avoided: avoidedSchema,
  halted: z.boolean(),
  walk_forward: walkForwardSchema.nullable(),
});
export type SymbolReport = z.infer<typeof symbolReportSchema>;

export const backtestReportSchema = z.looseObject({
  job_id: str,
  generated_at: nstr,
  disclaimer: str,
  assumptions: z.looseObject({
    capital: z.union([z.string(), z.number()]),
    taker_fee: z.union([z.string(), z.number()]),
    maker_fee: z.union([z.string(), z.number()]),
    slippage_bps: z.union([z.string(), z.number()]),
    latency_candles: num,
    timeframe: str,
    strategies: z.array(str),
    period: z.object({ start: str, end: str }),
  }),
  results: z.array(symbolReportSchema),
});
export type BacktestReport = z.infer<typeof backtestReportSchema>;
