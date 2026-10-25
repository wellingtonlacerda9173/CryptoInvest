import type { Calendar, CalendarEvent } from '@/api/calendarSchemas';

const DAY_MS = 86_400_000;
const BRASILIA = 'America/Sao_Paulo';

export const EVENT_COPY: Record<
  CalendarEvent['kind'],
  { chip: string; name: string; why: string }
> = {
  fomc: {
    chip: 'Juros EUA',
    name: 'Decisão de juros dos EUA (FOMC)',
    why: 'Os EUA anunciam os juros. Todo o mercado se mexe de uma vez.',
  },
  cpi: {
    chip: 'Inflação EUA',
    name: 'Inflação nos EUA (CPI)',
    why: 'Número da inflação nos EUA. Pode virar o preço em segundos.',
  },
  payroll: {
    chip: 'Emprego EUA',
    name: 'Emprego nos EUA (payroll)',
    why: 'Relatório de empregos nos EUA. Costuma causar saltos bruscos.',
  },
  deribit: {
    chip: 'Opções',
    name: 'Vencimento de opções (Deribit)',
    why: 'Dia em que muitos contratos vencem. Os preços podem oscilar sem aviso.',
  },
  other: {
    chip: 'Evento',
    name: '',
    why: 'Notícia ou data que costuma agitar o mercado.',
  },
};

export const eventName = (e: Pick<CalendarEvent, 'kind' | 'label'>) =>
  EVENT_COPY[e.kind].name || e.label;

/** Pausa EFETIVA do robo (janela com margens, vinda da API): [inicio, fim] inclusivo, como o gate. */
export const isActive = (e: Pick<CalendarEvent, 'block_start' | 'block_end'>, nowMs: number) =>
  Date.parse(e.block_start) <= nowMs && nowMs <= Date.parse(e.block_end);

export const activeEvent = (events: readonly CalendarEvent[], nowMs: number) =>
  events.find((e) => isActive(e, nowMs));

export const nextEvent = (events: readonly CalendarEvent[], nowMs: number) =>
  events.find((e) => Date.parse(e.block_start) > nowMs);

export type CalendarView = 'ok' | 'attention' | 'expiring' | 'expired';

/** Estado derivado no cliente: nunca mais verde que o servidor, e vencido assim que o relogio passa de valid_until. */
export function calendarView(
  c: Pick<Calendar, 'state' | 'valid_until'>,
  nowMs: number,
  opts: { hasActive: boolean; stale: boolean },
): CalendarView {
  const until = c.valid_until ? Date.parse(c.valid_until) : null;
  if (c.state === 'expired' || until === null || nowMs > until) return 'expired';
  if (c.state === 'expiring') return 'expiring';
  return opts.hasActive || opts.stale ? 'attention' : 'ok';
}

export const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
export const daysText = (n: number) => plural(n, 'dia', 'dias');

const dayKey = (ms: number, timeZone?: string) =>
  new Intl.DateTimeFormat('en-CA', { timeZone }).format(ms);

/** Dias de calendario (no fuso do usuario) entre agora e o instante; 0 = hoje, 1 = amanha. */
export function calendarDaysUntil(targetMs: number, nowMs: number): number {
  return Math.round((Date.parse(dayKey(targetMs)) - Date.parse(dayKey(nowMs))) / DAY_MS);
}

export const relativeDay = (targetMs: number, nowMs: number) => {
  const n = calendarDaysUntil(targetMs, nowMs);
  return n <= 0 ? 'Hoje' : n === 1 ? 'Amanhã' : `Em ${n} dias`;
};

export const lowerFirst = (s: string) => s.charAt(0).toLowerCase() + s.slice(1);

const hm = (ms: number, timeZone?: string) =>
  new Intl.DateTimeFormat('pt-BR', { hour: '2-digit', minute: '2-digit', timeZone }).format(ms);

export const brasiliaClock = (iso: string) => hm(Date.parse(iso), BRASILIA);
export const localClock = (iso: string | number) =>
  hm(typeof iso === 'number' ? iso : Date.parse(iso));

const dayLabel = (ms: number) =>
  new Intl.DateTimeFormat('pt-BR', { weekday: 'short', day: '2-digit', month: 'short' }).format(ms);
const weekday = (ms: number) => new Intl.DateTimeFormat('pt-BR', { weekday: 'short' }).format(ms);

/** "sex., 06 de nov., 09:30 às 11:30"; se cruzar a meia-noite local: "09:30 de sex. às 01:00 de sáb.". */
export function localRange(startIso: string, endIso: string) {
  const s = Date.parse(startIso);
  const e = Date.parse(endIso);
  if (dayKey(s) === dayKey(e)) return `${dayLabel(s)}, ${hm(s)} às ${hm(e)}`;
  return `${dayLabel(s)}, ${hm(s)} de ${weekday(s)} às ${hm(e)} de ${weekday(e)}`;
}

const utcDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const utcHm = (ms: number) => new Date(ms).toISOString().slice(11, 16);

export function utcRange(startIso: string, endIso: string) {
  const s = Date.parse(startIso);
  const e = Date.parse(endIso);
  return utcDay(s) === utcDay(e)
    ? `${utcDay(s)} ${utcHm(s)}–${utcHm(e)} UTC`
    : `${utcDay(s)} ${utcHm(s)} – ${utcDay(e)} ${utcHm(e)} UTC`;
}

export const utcSpoken = (startIso: string, endIso: string) =>
  `às ${utcHm(Date.parse(startIso))} a ${utcHm(Date.parse(endIso))}, horário UTC`;

export function userZone() {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const offset =
    new Intl.DateTimeFormat('pt-BR', { timeZoneName: 'shortOffset' })
      .formatToParts(Date.now())
      .find((p) => p.type === 'timeZoneName')?.value ?? 'GMT';
  const name = tz === BRASILIA ? 'Brasília' : (tz.split('/').pop() ?? tz).replace(/_/g, ' ');
  return `${name} (${offset.replace('GMT', 'UTC').replace('-', '−')})`;
}

const longDate = (ms: number) =>
  new Intl.DateTimeFormat('pt-BR', { day: 'numeric', month: 'long', year: 'numeric' }).format(ms);

export const validUntilText = (iso: string) => longDate(Date.parse(iso));

/** "hoje às 04:00", "ontem às 04:00" ou "13 de dezembro de 2026 às 04:00". */
export function pastMoment(iso: string, nowMs: number) {
  const ms = Date.parse(iso);
  const back = -calendarDaysUntil(ms, nowMs);
  const day = back <= 0 ? 'hoje' : back === 1 ? 'ontem' : longDate(ms);
  return `${day} às ${localClock(ms)}`;
}
