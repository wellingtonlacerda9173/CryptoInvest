import type { Candles, ChartDecision } from '@/api/marketSchemas';
import { makeCandles } from '@/test/marketData';

const HOUR = 3_600_000;

type Variant = 'ok' | 'parado';

export const chartVariantFromSearch = (search: string): Variant =>
  new URLSearchParams(search).get('grafico') === 'parado' ? 'parado' : 'ok';

const DECISIONS: Omit<ChartDecision, 'id' | 't'>[] = [
  {
    tier: 'entered',
    label: 'Entrou',
    text: 'O sinal passou por todos os filtros de risco e a ordem de compra foi enviada.',
    code: 'approved',
    strategy: 'elephant',
  },
  {
    tier: 'blocked',
    label: 'Não entrou',
    text: 'O calendário de eventos está vencido: o robô não abre entradas até ele ser atualizado.',
    code: 'calendar_stale',
    strategy: 'elephant',
  },
  {
    tier: 'blocked',
    label: 'Não entrou',
    text: 'O assistente de IA vetou esta entrada depois de ela passar pelos filtros de risco.',
    code: 'ai_veto',
    strategy: 'ema',
  },
  {
    tier: 'quiet',
    label: 'Esperou',
    text: 'Nenhuma estratégia viu uma boa oportunidade nesta vela. Ficar parado em USDT é uma decisão válida.',
    code: 'no_signal',
    strategy: null,
  },
];

/** Velas alinhadas à hora, perto do preço do ticker de mock, com decisões espalhadas. */
export function makeChartMock(variant: Variant, quiet: boolean, symbol: string): Candles {
  const base = Math.floor(Date.now() / HOUR) * HOUR;
  const lastOpen = base - (variant === 'parado' ? 6 : 1) * HOUR;
  const center = symbol.startsWith('ETH') ? 3_100 : 64_000;
  const count = 96;
  const starts = Array.from({ length: count }, (_, i) => lastOpen - (count - 1 - i) * HOUR);
  const closeAt = (i: number) => center * (1 + Math.sin(i / 6) * 0.012 + Math.sin(i / 2.3) * 0.002);
  const candles = starts.map((t, i) => {
    const o = closeAt(i - 1);
    const c = closeAt(i);
    return {
      t: new Date(t).toISOString(),
      o,
      c,
      h: Math.max(o, c) * 1.003,
      l: Math.min(o, c) * 0.997,
      v: 800 + ((i * 37) % 400),
    };
  });
  const all: ChartDecision[] = starts.flatMap((t, i) => {
    const pick = i % 11 === 0 ? 0 : i % 7 === 0 ? 1 : i % 13 === 0 ? 2 : 3;
    const d = DECISIONS[pick];
    return d ? [{ ...d, id: i + 1, t: new Date(t + 60_000).toISOString() }] : [];
  });
  const last = candles[candles.length - 1];
  return makeCandles({
    symbol,
    candles,
    decisions: quiet ? all : all.filter((d) => d.tier !== 'quiet'),
    markers: [],
    levels: [],
    last_price: last?.c ?? null,
  });
}
