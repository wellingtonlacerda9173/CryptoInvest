import { z } from 'zod';
import type { Schemas } from './schemas';

type Typed<K extends keyof Schemas> = z.ZodType<Schemas[K]>;

const str = z.string();
const num = z.number();
const nstr = z.string().nullable();
const nnum = z.number().nullable();
const modeEnum = z.enum(['paper', 'live', 'backtest']);

export const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d'] as const;
export type Timeframe = (typeof TIMEFRAMES)[number];

export const tickerSchema: Typed<'TickerOut'> = z.object({
  quotes: z.array(
    z.object({
      symbol: str,
      price: num,
      change_24h_pct: num,
      as_of: str,
      age_seconds: num,
      stale: z.boolean(),
    }),
  ),
});

export const candlesSchema: Typed<'CandlesOut'> = z.object({
  mode: modeEnum,
  simulated: z.boolean(),
  symbol: str,
  timeframe: str,
  candles: z.array(z.object({ t: str, o: num, h: num, l: num, c: num, v: num })),
  markers: z.array(
    z.object({
      t: str,
      kind: z.enum(['entry', 'exit', 'stop', 'target', 'elephant']),
      price: nnum,
      text: str,
      run_id: nstr.optional(),
    }),
  ),
  levels: z.array(z.object({ kind: z.enum(['stop', 'target', 'entry']), price: num, since: str })),
  last_price: nnum,
  change_24h_pct: nnum,
  volume_24h: nnum,
  last_decision: nstr,
  blind_since: nstr.optional(),
  decisions: z
    .array(
      z.object({
        id: num,
        t: str,
        tier: z.enum(['entered', 'blocked', 'quiet']),
        label: str,
        text: str,
        code: str,
        strategy: nstr,
      }),
    )
    .optional(),
  decisions_truncated: z.boolean().default(false),
});
export type ChartDecision = Schemas['ChartDecisionOut'];
export type Candles = Schemas['CandlesOut'];
export type CandleBar = Schemas['CandleOut'];
export type CandleMarker = Schemas['MarkerOut'];

export const tradeSchema: Typed<'TradeOut'> = z.object({
  id: num,
  trade_id: str,
  run_id: str,
  mode: modeEnum,
  symbol: str,
  strategy: nstr,
  opened_at: str,
  closed_at: str,
  entry_price: num,
  exit_price: num,
  quantity: num,
  fees: num,
  pnl: num,
  r_multiple: nnum,
  exit_reason: str,
  exit_reason_text: str,
  result: z.enum(['win', 'loss', 'breakeven']),
});
export type Trade = Schemas['TradeOut'];

export const tradePageSchema: Typed<'TradePage'> = z.object({
  items: z.array(tradeSchema),
  next_cursor: nnum.optional(),
  summary: z.object({ count: num, wins: num, losses: num, pnl: num, fees: num }),
});
export type TradePage = Schemas['TradePage'];

const groupStatsSchema = z.object({
  key: str,
  trades: num,
  wins: num,
  pnl: num,
  win_rate: nnum,
  profit_factor: nnum,
});
export type GroupStats = Schemas['GroupStatsOut'];

export const performanceSchema: Typed<'PerformanceOut'> = z.object({
  mode: modeEnum,
  simulated: z.boolean(),
  run_id: nstr,
  period_start: nstr,
  period_end: str,
  trades: num,
  wins: num,
  losses: num,
  signals_refused: num,
  total_pnl: num,
  total_return_pct: nnum,
  max_drawdown_pct: nnum,
  profit_factor: nnum,
  expectancy_r: nnum,
  win_rate: nnum,
  payoff: nnum,
  sharpe: nnum,
  idle_pct: nnum,
  months: num,
  positive_months: num,
  by_month: z.array(groupStatsSchema),
  by_pair: z.array(groupStatsSchema),
  by_strategy: z.array(groupStatsSchema),
  avoided: z.array(z.object({ code: str, count: num, label: str })),
  notes: z.array(str),
  sample_warning: nstr,
});
export type Performance = Schemas['PerformanceOut'];

/** Parecer extra do robo dentro de `decision.signal` (JSON livre): le so texto e numeros, com limite de tamanho. */
export interface SignalInsight {
  regime: string | null;
  scores: { strategy: string; score: number }[];
  ai: string | null;
  aiDetail: { verdict: 'approve' | 'veto'; confidence: number; reason: string } | null;
  levels: { entry: number | null; stop: number | null; target: number | null };
}

const asNumber = (v: unknown): number | null => {
  const n = typeof v === 'string' ? Number(v) : v;
  return typeof n === 'number' && Number.isFinite(n) ? n : null;
};
const asText = (v: unknown, max = 600): string | null =>
  typeof v === 'string' && v.trim() ? v.trim().slice(0, max) : null;

function readAiDetail(raw: unknown): SignalInsight['aiDetail'] {
  if (typeof raw !== 'object' || raw === null) return null;
  const d = raw as Record<string, unknown>;
  const confidence = asNumber(d.confidence);
  const reason = asText(d.reason, 280);
  if ((d.verdict !== 'approve' && d.verdict !== 'veto') || confidence === null || !reason)
    return null;
  return { verdict: d.verdict, confidence, reason };
}

export function readSignal(signal: Record<string, unknown> | null): SignalInsight {
  const s = signal ?? {};
  const raw = s.scores ?? s.evaluated;
  const scores: SignalInsight['scores'] = [];
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (typeof item !== 'object' || item === null) continue;
      const e = item as Record<string, unknown>;
      const name = asText(e.strategy ?? e.name, 80);
      const score = asNumber(e.score ?? e.confidence);
      if (name && score !== null) scores.push({ strategy: name, score });
    }
  } else if (typeof raw === 'object' && raw !== null) {
    for (const [name, value] of Object.entries(raw)) {
      const score = asNumber(value);
      if (score !== null) scores.push({ strategy: name.slice(0, 80), score });
    }
  }
  return {
    regime: asText(s.regime, 80),
    scores,
    ai: asText(s.ai_opinion ?? s.ai),
    aiDetail: readAiDetail(s.ai_detail),
    levels: {
      entry: asNumber(s.entry),
      stop: asNumber(s.stop_loss),
      target: asNumber(s.take_profit),
    },
  };
}
