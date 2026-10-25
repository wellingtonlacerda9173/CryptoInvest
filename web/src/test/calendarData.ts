import type { Calendar, CalendarEvent } from '@/api/calendarSchemas';

export type CalendarVariant = 'ok' | 'ativo' | 'expirando' | 'vencido';

const H = 3_600_000;
const D = 24 * H;
const iso = (ms: number) => new Date(ms).toISOString().replace('.000Z', 'Z');

const ev = (
  kind: CalendarEvent['kind'],
  label: string,
  source: string,
  startMs: number,
  hours: number,
): CalendarEvent => ({
  kind,
  label,
  source,
  start: iso(startMs),
  end: iso(startMs + hours * H),
  block_start: iso(startMs - 0.5 * H),
  block_end: iso(startMs + hours * H + 0.5 * H),
});

const upcoming = (nowMs: number, active: boolean): CalendarEvent[] => [
  ev(
    'payroll',
    'EUA payroll (nov)',
    'BLS (governo dos EUA)',
    active ? nowMs - 0.5 * H : nowMs + 3 * D,
    2,
  ),
  ev('cpi', 'EUA CPI (nov)', 'BLS (governo dos EUA)', nowMs + 9 * D, 2),
  ev(
    'fomc',
    'FOMC decisao de juros (dez)',
    'Federal Reserve (banco central dos EUA)',
    nowMs + 20 * D,
    3,
  ),
  ev('deribit', 'Deribit vencimento mensal (dez)', 'Deribit', nowMs + 33 * D, 2),
  ev('other', 'Evento especial', 'Calendario do robo', nowMs + 40 * D, 2),
];

/** Dados ficticios do calendario nos estados da tela; `nowMs` fixo deixa os testes deterministicos. */
export function makeCalendar(variant: CalendarVariant, nowMs = Date.now()): Calendar {
  const base = {
    now: iso(nowMs),
    loaded_at: iso(nowMs - 20 * D),
    last_success_at: iso(nowMs - 20 * D),
  };
  if (variant === 'vencido')
    return {
      ...base,
      state: 'expired',
      valid_until: iso(nowMs - 3 * D),
      days_remaining: null,
      margin_before_minutes: 30,
      margin_after_minutes: 30,
      upcoming: [],
      last_update: { attempted_at: iso(nowMs - 5 * H), result: 'failed' },
    };
  const expiring = variant === 'expirando';
  const days = expiring ? 9 : 81;
  return {
    ...base,
    state: expiring ? 'expiring' : 'ok',
    valid_until: iso(nowMs + days * D),
    days_remaining: days,
    margin_before_minutes: 30,
    margin_after_minutes: 30,
    upcoming: upcoming(nowMs, variant === 'ativo'),
    last_update: {
      attempted_at: iso(nowMs - 8 * H),
      result: expiring ? 'unchanged' : 'updated',
    },
  };
}

export const calendarVariantFromSearch = (search: string): CalendarVariant => {
  const v = new URLSearchParams(search).get('calendario');
  return v === 'ativo' || v === 'expirando' || v === 'vencido' ? v : 'ok';
};
