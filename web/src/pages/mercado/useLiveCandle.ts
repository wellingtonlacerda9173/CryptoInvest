import { useEffect, useMemo, useState } from 'react';
import { useTicker } from '@/api/marketQueries';
import type { CandleBar, Timeframe } from '@/api/marketSchemas';
import {
  TF_SECONDS,
  foldTick,
  freshness,
  mergeLive,
  type Forming,
  type Freshness,
  type LiveView,
} from '@/lib/liveCandle';
import { resolveLiveState } from '@/lib/ticker';

const EMPTY: LiveView = { bars: [], gaps: [], hasLive: false, livePrice: null };

function useClock(intervalMs: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/**
 * Vela em formacao a partir do ticker (o mesmo do preco grande, mesma consulta em cache).
 * So usa cotacao "ao vivo"; atrasada ou ausente nao desenha vela nenhuma. A maxima e a minima da vela em
 * formacao sao aproximadas: vem de amostras do ticker (a cada ~3 s), nao de cada negocio.
 * O instante da amostra e o `as_of` da cotacao, nao o da chegada da resposta.
 */
export function useLiveCandle(
  symbol: string | undefined,
  symbols: readonly string[],
  timeframe: Timeframe,
  candles: readonly CandleBar[],
): { view: LiveView; fresh: Freshness; now: number } {
  const ticker = useTicker(symbols, symbol);
  const now = useClock(5_000);
  const tfSec = TF_SECONDS[timeframe];
  const [state, setState] = useState<{ key: string; stamp: number; forming: Forming[] }>({
    key: '',
    stamp: 0,
    forming: [],
  });
  const key = `${symbol ?? ''}|${timeframe}`;
  const quote = ticker.data?.quotes.find((q) => q.symbol === symbol);
  const live = resolveLiveState(quote, ticker.dataUpdatedAt, now) === 'live';
  const price = live ? quote?.price : undefined;
  const asOfParsed = Date.parse(quote?.as_of ?? '');
  const asOf = Number.isFinite(asOfParsed) ? asOfParsed : ticker.dataUpdatedAt;

  if (price !== undefined && candles.length > 0 && (state.key !== key || state.stamp !== asOf)) {
    const base = state.key === key ? state.forming : [];
    setState({
      key,
      stamp: asOf,
      forming: foldTick(base, price, asOf, tfSec, candles),
    });
  }

  const forming = useMemo(() => (state.key === key ? state.forming : []), [state, key]);
  const view = useMemo(
    () => (candles.length === 0 ? EMPTY : mergeLive(candles, forming, tfSec, now)),
    [candles, forming, tfSec, now],
  );
  return { view, fresh: freshness(candles, tfSec, now), now };
}
