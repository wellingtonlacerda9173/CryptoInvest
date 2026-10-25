import type { CandleBar, Timeframe } from '@/api/marketSchemas';

export const TF_SECONDS: Record<Timeframe, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '30m': 1800,
  '1h': 3600,
  '4h': 14_400,
  '1d': 86_400,
};

/** Quanto depois do fechamento a vela em formação (ja encerrada) espera a vela oficial do banco. */
export const GRACE_MS = 120_000;
export const MAX_GAP_FILL = 400;

export interface Forming {
  /** Inicio da vela, em ms. */
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
}

export interface Freshness {
  stale: boolean;
  /** Fim da ultima vela gravada (ms): ate quando o banco tem dado. */
  lastDataAt: number | null;
}

const ms = (iso: string) => new Date(iso).getTime();

/** As velas abrem em multiplos do periodo contados da meia-noite UTC (1 dia = 00:00 UTC). */
export const bucketStart = (atMs: number, tfSec: number) => {
  const size = tfSec * 1000;
  return Math.floor(atMs / size) * size;
};

export function freshness(candles: readonly CandleBar[], tfSec: number, nowMs: number): Freshness {
  const last = candles[candles.length - 1];
  if (!last) return { stale: false, lastDataAt: null };
  const opened = ms(last.t);
  return { stale: nowMs - opened > 2 * tfSec * 1000, lastDataAt: opened + tfSec * 1000 };
}

/**
 * Soma um preco ao vivo as velas em formacao. A abertura e o fechamento da vela anterior quando ela e
 * a vizinha (banco ou observada); sem vizinha (lacuna ou 1o tick) e o proprio tick. Nunca inventa passado.
 */
export function foldTick(
  forming: readonly Forming[],
  price: number,
  tickMs: number,
  tfSec: number,
  db: readonly CandleBar[],
): Forming[] {
  if (!Number.isFinite(price) || price <= 0) return [...forming];
  const t = bucketStart(tickMs, tfSec);
  const size = tfSec * 1000;
  const kept = forming.filter((f) => f.t >= t - size && f.t <= t);
  const current = kept.find((f) => f.t === t);
  if (current) {
    return kept.map((f) =>
      f === current ? { ...f, h: Math.max(f.h, price), l: Math.min(f.l, price), c: price } : f,
    );
  }
  const previous = kept.find((f) => f.t === t - size);
  const dbPrevious = db.find((k) => ms(k.t) === t - size);
  const open = previous?.c ?? dbPrevious?.c ?? price;
  return [...kept, { t, o: open, h: Math.max(open, price), l: Math.min(open, price), c: price }];
}

export interface BarView {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  forming: boolean;
}

export interface LiveView {
  bars: BarView[];
  /** Horarios (ms) sem vela entre dois pontos, para desenhar a lacuna em vez de esconder. */
  gaps: number[];
  /** Ha vela em formação desenhada. */
  hasLive: boolean;
  /** Preco da vela em formação (linha de preco atual). */
  livePrice: number | null;
}

/**
 * Junta as velas oficiais do banco com as em formacao. Vela do banco do periodo em curso e estendida pelo
 * ticker; periodo ja encerrado com vela oficial no banco descarta a nossa (a oficial vale).
 */
export function mergeLive(
  db: readonly CandleBar[],
  forming: readonly Forming[],
  tfSec: number,
  nowMs: number,
): LiveView {
  const size = tfSec * 1000;
  const bars: BarView[] = db.map((k) => ({
    t: ms(k.t),
    o: k.o,
    h: k.h,
    l: k.l,
    c: k.c,
    forming: false,
  }));
  const index = new Map(bars.map((b, i) => [b.t, i]));
  let livePrice: number | null = null;
  for (const f of [...forming].sort((a, b) => a.t - b.t)) {
    const closed = nowMs >= f.t + size;
    if (closed && nowMs > f.t + size + GRACE_MS) continue;
    const at = index.get(f.t);
    if (at !== undefined) {
      if (closed) continue;
      const official = bars[at];
      if (!official) continue;
      bars[at] = {
        ...official,
        h: Math.max(official.h, f.h),
        l: Math.min(official.l, f.l),
        c: f.c,
        forming: true,
      };
    } else {
      const newest = bars[bars.length - 1];
      if (newest && f.t < newest.t) continue;
      bars.push({ ...f, forming: true });
    }
    if (!closed) livePrice = f.c;
  }
  bars.sort((a, b) => a.t - b.t);
  const gaps: number[] = [];
  for (let i = 1; i < bars.length && gaps.length < MAX_GAP_FILL; i++) {
    const prev = bars[i - 1];
    const next = bars[i];
    if (!prev || !next) continue;
    for (let t = prev.t + size; t < next.t && gaps.length < MAX_GAP_FILL; t += size) gaps.push(t);
  }
  return { bars, gaps, hasLive: bars.some((b) => b.forming), livePrice };
}

const hhmm = (atMs: number) =>
  new Intl.DateTimeFormat('pt-BR', { hour: '2-digit', minute: '2-digit' }).format(atMs);

export const staleMessage = (lastDataAt: number | null) =>
  lastDataAt === null
    ? 'O gráfico está desatualizado: o robô não está gravando velas.'
    : `O gráfico está desatualizado: o robô não está gravando velas (último dado às ${hhmm(lastDataAt)}).`;
