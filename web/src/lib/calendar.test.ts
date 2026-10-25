import { beforeAll, describe, expect, it } from 'vitest';
import {
  activeEvent,
  calendarView,
  calendarDaysUntil,
  daysText,
  isActive,
  localRange,
  relativeDay,
  utcRange,
} from './calendar';

beforeAll(() => {
  process.env.TZ = 'America/Sao_Paulo';
});

const win = {
  start: '2026-11-06T12:30:00Z',
  end: '2026-11-06T14:30:00Z',
  block_start: '2026-11-06T12:00:00Z',
  block_end: '2026-11-06T15:00:00Z',
};
const at = (iso: string) => Date.parse(iso);

describe('calendario', () => {
  it('pausa efetiva (com margens): inicio e fim inclusivos, como o gate', () => {
    expect(isActive(win, at('2026-11-06T11:59:59Z'))).toBe(false);
    expect(isActive(win, at('2026-11-06T12:00:00Z'))).toBe(true);
    expect(isActive(win, at('2026-11-06T14:45:00Z'))).toBe(true);
    expect(isActive(win, at('2026-11-06T15:00:00Z'))).toBe(true);
    expect(isActive(win, at('2026-11-06T15:00:01Z'))).toBe(false);
    const e = { ...win, kind: 'payroll' as const, label: 'x', source: 's' };
    expect(activeEvent([e], at('2026-11-06T13:00:00Z'))).toBe(e);
  });
  it('estado derivado: vencido pelo relogio, atencao com janela ativa/erro, nunca verde sem dados frescos', () => {
    const c = { state: 'ok' as const, valid_until: '2026-12-01T00:00:00Z' };
    const calm = { hasActive: false, stale: false };
    expect(calendarView(c, at('2026-11-30T23:59:59Z'), calm)).toBe('ok');
    expect(calendarView(c, at('2026-12-01T00:00:01Z'), calm)).toBe('expired');
    expect(calendarView(c, at('2026-11-06T12:00:00Z'), { ...calm, hasActive: true })).toBe(
      'attention',
    );
    expect(calendarView(c, at('2026-11-06T12:00:00Z'), { ...calm, stale: true })).toBe('attention');
    expect(calendarView({ ...c, valid_until: null }, 0, calm)).toBe('expired');
    expect(calendarView({ ...c, state: 'expired' }, 0, calm)).toBe('expired');
  });
  it('singular e plural', () => {
    expect(daysText(1)).toBe('1 dia');
    expect(daysText(0)).toBe('0 dias');
    expect(daysText(9)).toBe('9 dias');
  });
  it('dia relativo no fuso do usuario', () => {
    const now = at('2026-11-03T15:00:00Z');
    expect(relativeDay(at('2026-11-03T23:00:00Z'), now)).toBe('Hoje');
    expect(relativeDay(at('2026-11-04T12:00:00Z'), now)).toBe('Amanhã');
    expect(relativeDay(at('2026-11-12T12:00:00Z'), now)).toBe('Em 9 dias');
    expect(calendarDaysUntil(at('2026-11-04T01:00:00Z'), now)).toBe(0);
  });
  it('mostra hora local e UTC; cruzando a meia-noite local explica os dois dias', () => {
    expect(localRange(win.start, win.end)).toMatch(/06 de nov\.?, 09:30 às 11:30/);
    expect(utcRange(win.start, win.end)).toBe('2026-11-06 12:30–14:30 UTC');
    const night = localRange('2026-11-07T02:30:00Z', '2026-11-07T04:00:00Z');
    expect(night).toMatch(/23:30 de .* às 01:00 de /);
  });
});
