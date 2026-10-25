import { describe, expect, it } from 'vitest';
import type { CandleBar } from '@/api/marketSchemas';
import {
  GRACE_MS,
  MAX_GAP_FILL,
  bucketStart,
  foldTick,
  freshness,
  mergeLive,
  staleMessage,
} from './liveCandle';

const H = 3600;
const HMS = H * 1000;
const T0 = Date.UTC(2026, 9, 25, 12, 0, 0);

const bar = (openMs: number, o: number, c: number): CandleBar => ({
  t: new Date(openMs).toISOString(),
  o,
  h: Math.max(o, c) + 1,
  l: Math.min(o, c) - 1,
  c,
  v: 10,
});

describe('bucketStart', () => {
  it('alinha ao inicio do periodo em UTC', () => {
    expect(bucketStart(T0 + 59 * 60_000, H)).toBe(T0);
    expect(bucketStart(T0 + 17 * 60_000, 900)).toBe(T0 + 15 * 60_000);
    expect(bucketStart(T0 + 5 * HMS, 86_400)).toBe(Date.UTC(2026, 9, 25));
  });
});

describe('foldTick', () => {
  it('abre na vela anterior do banco e acompanha maxima e minima', () => {
    const db = [bar(T0 - HMS, 100, 105)];
    let f = foldTick([], 108, T0 + 60_000, H, db);
    expect(f).toEqual([{ t: T0, o: 105, h: 108, l: 105, c: 108 }]);
    f = foldTick(f, 103, T0 + 120_000, H, db);
    expect(f[0]).toMatchObject({ o: 105, h: 108, l: 103, c: 103 });
  });

  it('sem vela vizinha (lacuna), abre no primeiro tick: nao inventa passado', () => {
    const db = [bar(T0 - 6 * HMS, 100, 105)];
    expect(foldTick([], 120, T0, H, db)).toEqual([{ t: T0, o: 120, h: 120, l: 120, c: 120 }]);
  });

  it('virada de hora abre a nova vela no fechamento da anterior observada e guarda so duas', () => {
    const db = [bar(T0 - HMS, 100, 105)];
    let f = foldTick([], 110, T0 + 30 * 60_000, H, db);
    f = foldTick(f, 112, T0 + HMS + 1000, H, db);
    expect(f.map((x) => x.t)).toEqual([T0, T0 + HMS]);
    expect(f[1]).toMatchObject({ o: 110, c: 112, h: 112 });
    f = foldTick(f, 113, T0 + 2 * HMS + 1000, H, db);
    expect(f.map((x) => x.t)).toEqual([T0 + HMS, T0 + 2 * HMS]);
  });

  it('ignora preco invalido', () => {
    expect(foldTick([], Number.NaN, T0, H, [])).toEqual([]);
    expect(foldTick([], 0, T0, H, [])).toEqual([]);
  });
});

describe('mergeLive', () => {
  const db = [bar(T0 - 2 * HMS, 98, 100), bar(T0 - HMS, 100, 105)];

  it('acrescenta a vela em formacao da hora corrente depois da ultima oficial', () => {
    const f = foldTick([], 108, T0 + 60_000, H, db);
    const v = mergeLive(db, f, H, T0 + 60_000);
    expect(v.bars).toHaveLength(3);
    expect(v.bars[2]).toMatchObject({ t: T0, forming: true, c: 108, o: 105 });
    expect(v.bars.slice(0, 2).every((b) => !b.forming)).toBe(true);
    expect(v.gaps).toEqual([]);
    expect(v.livePrice).toBe(108);
  });

  it('estende a vela do banco quando ela ja e a da hora corrente', () => {
    const current = [...db, { ...bar(T0, 105, 106), h: 107, l: 104 }];
    const f = foldTick([], 110, T0 + 60_000, H, current);
    const last = mergeLive(current, f, H, T0 + 60_000).bars[2];
    expect(last).toMatchObject({ forming: true, o: 105, h: 110, l: 104, c: 110 });
  });

  it('a vela oficial vale quando o periodo encerrou', () => {
    const f = foldTick([], 108, T0 + 60_000, H, db);
    const official = [...db, bar(T0, 105, 109)];
    const v = mergeLive(official, f, H, T0 + HMS + 5_000);
    expect(v.bars).toHaveLength(3);
    expect(v.bars[2]).toMatchObject({ forming: false, c: 109 });
    expect(v.hasLive).toBe(false);
  });

  it('mantem a vela encerrada ainda sem oficial so durante a tolerancia', () => {
    const f = foldTick([], 108, T0 + 60_000, H, db);
    expect(mergeLive(db, f, H, T0 + HMS + 10_000).bars).toHaveLength(3);
    expect(mergeLive(db, f, H, T0 + HMS + GRACE_MS + 1).bars).toHaveLength(2);
  });

  it('mostra a lacuna entre a ultima vela antiga e a vela atual', () => {
    const old = [bar(T0 - 5 * HMS, 100, 101)];
    const f = foldTick([], 120, T0 + 60_000, H, old);
    const v = mergeLive(old, f, H, T0 + 60_000);
    expect(v.bars).toHaveLength(2);
    expect(v.gaps).toEqual([1, 2, 3, 4].map((n) => T0 - 5 * HMS + n * HMS));
    expect(v.bars[1]).toMatchObject({ o: 120 });
  });

  it('limita o preenchimento de lacunas', () => {
    const ancient = [bar(T0 - 5000 * HMS, 1, 1)];
    const f = foldTick([], 2, T0, H, ancient);
    expect(mergeLive(ancient, f, H, T0).gaps).toHaveLength(MAX_GAP_FILL);
  });

  it('sem cotacao ao vivo nao desenha nada alem do banco', () => {
    const v = mergeLive(db, [], H, T0);
    expect(v.bars).toHaveLength(2);
    expect(v.hasLive).toBe(false);
    expect(v.livePrice).toBeNull();
  });
});

describe('freshness', () => {
  const db = [bar(T0 - HMS, 100, 105)];
  it('atual ate 2x o periodo depois da abertura da ultima vela', () => {
    expect(freshness(db, H, T0 + HMS).stale).toBe(false);
    expect(freshness(db, H, T0 + HMS + 1).stale).toBe(true);
  });
  it('informa ate quando ha dado e nao acusa atraso sem velas', () => {
    expect(freshness(db, H, T0).lastDataAt).toBe(T0);
    expect(freshness([], H, T0)).toEqual({ stale: false, lastDataAt: null });
  });
  it('texto pt-BR com o horario do ultimo dado', () => {
    expect(staleMessage(T0)).toMatch(
      /^O gráfico está desatualizado: o robô não está gravando velas \(último dado às \d{2}:\d{2}\)\.$/,
    );
    expect(staleMessage(null)).toContain('desatualizado');
  });
});
