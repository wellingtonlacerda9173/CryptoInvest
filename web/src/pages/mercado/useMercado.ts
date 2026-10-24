import { useMemo, useState } from 'react';
import { useCandles } from '@/api/marketQueries';
import { TIMEFRAMES, type Timeframe } from '@/api/marketSchemas';
import { usePositions, useSettings } from '@/api/queries';
import type { ChartLevel } from '@/components/kit/CandleChart';
import { useConnection } from '@/realtime/ConnectionProvider';
import { formatNumber } from '@/lib/format';

const isTimeframe = (v: string): v is Timeframe => (TIMEFRAMES as readonly string[]).includes(v);
const LEVEL_TITLE = { stop: 'Stop', target: 'Alvo', entry: 'Entrada' } as const;

export function useMercado() {
  const settings = useSettings();
  const positions = usePositions();
  const { disconnected } = useConnection();
  const [chosenSymbol, setSymbol] = useState<string>();
  const [chosenTf, setTimeframe] = useState<Timeframe>();
  const [showElephants, setShowElephants] = useState(true);
  const [showLevels, setShowLevels] = useState(true);

  const configured = settings.data?.document.pairs.symbols;
  const symbols = useMemo(() => {
    const set = new Set<string>(configured ?? []);
    (positions.data ?? []).forEach((p) => set.add(p.symbol));
    return [...set];
  }, [configured, positions.data]);

  const symbol = chosenSymbol && symbols.includes(chosenSymbol) ? chosenSymbol : symbols[0];
  const defaultTf = settings.data?.document.pairs.timeframe ?? '1h';
  const timeframe = chosenTf ?? (isTimeframe(defaultTf) ? defaultTf : '1h');
  const candles = useCandles(symbol, timeframe);
  const position = positions.data?.find((p) => p.symbol === symbol);

  const levels = useMemo<ChartLevel[]>(
    () =>
      showLevels
        ? (candles.data?.levels ?? []).map((l) => ({
            price: l.price,
            title: LEVEL_TITLE[l.kind],
            tone: l.kind,
          }))
        : [],
    [candles.data?.levels, showLevels],
  );

  const bars = candles.data?.candles ?? [];
  const last = bars[bars.length - 1];
  const summary = last
    ? `Gráfico de velas de ${symbol ?? ''}, ${bars.length} velas de ${timeframe}, último fechamento ${formatNumber(last.c)}. Os mesmos dados estão na tabela das últimas velas.`
    : 'Gráfico de velas sem dados.';

  return {
    symbols,
    symbol,
    setSymbol,
    timeframe,
    setTimeframe,
    showElephants,
    setShowElephants,
    showLevels,
    setShowLevels,
    candles,
    position,
    levels,
    disconnected,
    summary,
  };
}
