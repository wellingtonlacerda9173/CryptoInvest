import { describe, expect, it } from 'vitest';
import type { ChartDecision } from '@/api/marketSchemas';
import { MAX_DECISIONS, decisionSentence, groupDecisions } from './chartDecisions';

const T0 = Date.UTC(2026, 9, 25, 12) / 1000;
const times = [T0, T0 + 3600, T0 + 7200];

const dec = (id: number, atSec: number, tier: ChartDecision['tier']): ChartDecision => ({
  id,
  t: new Date(atSec * 1000).toISOString(),
  tier,
  label: tier === 'entered' ? 'Entrou' : tier === 'blocked' ? 'Não entrou' : 'Esperou',
  text: 'Motivo.',
  code: 'x',
  strategy: null,
});

describe('groupDecisions', () => {
  it('agrupa por vela e a marca mais forte vence', () => {
    const g = groupDecisions(
      [dec(1, T0 + 60, 'quiet'), dec(2, T0 + 120, 'blocked'), dec(3, T0 + 180, 'quiet')],
      times,
      3600,
      true,
    );
    expect(g).toHaveLength(1);
    expect(g[0]).toMatchObject({ time: T0, tier: 'blocked' });
    expect(g[0]?.items).toHaveLength(3);
  });

  it('decisão no período em curso cai na vela em formação, não na seguinte', () => {
    const formingStart = T0 + 10800;
    const withForming = [...times, formingStart];
    const list = [dec(1, formingStart + 120, 'entered')];
    expect(groupDecisions(list, times, 3600, true)).toEqual([]);
    expect(groupDecisions(list, withForming, 3600, true)).toMatchObject([{ time: formingStart }]);
  });

  it('esconde "sem sinal" quando o interruptor esta desligado', () => {
    const list = [dec(1, T0 + 60, 'quiet'), dec(2, T0 + 3700, 'entered')];
    expect(groupDecisions(list, times, 3600, false).map((x) => x.tier)).toEqual(['entered']);
    expect(groupDecisions(list, times, 3600, true)).toHaveLength(2);
  });

  it('descarta decisao fora das velas mostradas ou depois de uma lacuna', () => {
    const list = [dec(1, T0 - 10, 'entered'), dec(2, T0 + 7200 + 4000, 'entered')];
    expect(groupDecisions(list, times, 3600, true)).toEqual([]);
  });

  it('limita a quantidade processada e ordena por tempo', () => {
    const many = Array.from({ length: MAX_DECISIONS + 50 }, (_, i) => dec(i, T0 + 7200, 'quiet'));
    const [g] = groupDecisions(many, times, 3600, true);
    expect(g?.items).toHaveLength(MAX_DECISIONS);
    const out = groupDecisions(
      [dec(1, T0 + 7300, 'entered'), dec(2, T0 + 10, 'entered')],
      times,
      3600,
      true,
    );
    expect(out.map((x) => x.time)).toEqual([T0, T0 + 7200]);
  });
});

describe('decisionSentence', () => {
  it('monta a frase traduzida', () => {
    expect(decisionSentence(dec(1, T0, 'blocked'))).toBe('Não entrou: Motivo.');
    expect(decisionSentence(dec(2, T0, 'entered'))).toBe('Entrou: Motivo.');
  });
});
