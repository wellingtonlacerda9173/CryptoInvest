import { z } from 'zod';
import type { Schemas } from './schemas';

type Typed<K extends keyof Schemas> = z.ZodType<Schemas[K]>;

const str = z.string();
const num = z.number();
const nstr = z.string().nullable();
const nnum = z.number().nullable();

export const aiUsageSchema: Typed<'AiUsageOut'> = z.object({
  month_start: str,
  spent_usd: num,
  budget_usd: nnum,
  calls: num,
  failures: num,
  budget_stops: num,
  by_result: z.record(str, num),
  runner_status: nstr,
  circuit_open: z.boolean().nullable(),
  calls_last_hour: nnum,
});
export type AiUsage = Schemas['AiUsageOut'];

const observationSchema: Typed<'ObservationOut'> = z.object({
  text: str,
  evidence: str,
  confidence: z.enum(['low', 'medium', 'high']),
});

const lessonShape = {
  id: str,
  created_at: str,
  period_start: str,
  period_end: str,
  source: z.enum(['ai', 'deterministic']),
  model: nstr,
  prompt_version: str,
  status: z.enum(['pending', 'approved', 'rejected']),
  reviewer: nstr,
  review_reason: nstr,
  reviewed_at: nstr,
  summary: str,
  observations: z.array(observationSchema),
};

export const lessonSchema: Typed<'LessonOut'> = z.object(lessonShape);
export type Lesson = Schemas['LessonOut'];
export type Observation = Schemas['ObservationOut'];

const groupStatsSchema: Typed<'LessonGroupOut'> = z.object({
  key: str,
  trades: num,
  wins: num,
  losses: num,
  total_r: num,
  mean_r: num,
  loss_r: num,
  worst_r: num,
  max_drawdown_r: num,
});

export const lessonDetailSchema: Typed<'LessonDetailOut'> = z.object({
  ...lessonShape,
  stats: z.object({
    trades: num,
    wins: num,
    losses: num,
    total_r: num,
    loss_r: num,
    skipped: num,
    by_strategy: z.array(groupStatsSchema),
    by_regime: z.array(groupStatsSchema),
    by_symbol: z.array(groupStatsSchema),
    worst_losses: z.array(
      z.object({ strategy: str, symbol: str, regime: str, closed_on: str, r_multiple: num }),
    ),
    decisions: z.record(str, num),
  }),
});
export type LessonDetail = Schemas['LessonDetailOut'];
export type LessonStatus = Lesson['status'];

export const lessonPageSchema: Typed<'LessonPage'> = z.object({
  items: z.array(lessonSchema),
  next_cursor: nstr.optional(),
});

export const lessonRunSchema: Typed<'LessonRunOut'> = z.object({
  state: z.literal('started'),
  message: str,
});

export const aiValueSchema: Typed<'AiValueOut'> = z.object({
  period_start: str,
  period_end: str,
  calls: num,
  failed_calls: num,
  cost_usd: num,
  vetoes: num,
  simulated: num,
  not_filled: num,
  unresolved: num,
  not_simulated: num,
  avoided_loss: num,
  missed_gain: num,
  net_avoided: num,
  correct_vetoes: num,
  false_vetoes: num,
  false_veto_rate: nnum,
  mean_r_vetoed: nnum,
  approved_trades: num,
  approved_wins: num,
  approved_losses: num,
  approved_pnl: num,
  approved_loss_pnl: num,
  approved_total_r: num,
  net_value_usd: num,
  verdict: z.enum([
    'no_activity',
    'insufficient_sample',
    'no_conclusion',
    'ai_avoided_losses',
    'ai_missed_gains',
    'no_difference',
  ]),
  verdict_text: str,
  small_sample: z.boolean(),
  cases: z.array(
    z.object({
      decided_at: str,
      code: str,
      strategy: str,
      symbol: str,
      status: str,
      pnl: num,
      r_multiple: num,
    }),
  ),
});
export type AiValue = Schemas['AiValueOut'];
