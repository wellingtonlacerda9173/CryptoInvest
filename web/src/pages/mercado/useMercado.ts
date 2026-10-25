import { useMemo, useState } from 'react';
import { useCandles } from '@/api/marketQueries';
import { TIMEFRAMES, type Timeframe } from '@/api/marketSchemas';
import { usePositions, useSettings } from '@/api/queries';
import type { ChartLevel } from '@/components/kit/CandleChart';
import { useConnection } from '@/realtime/ConnectionProvider';
import { formatNumber } from '@/lib/format';
import { groupDecisions } from '@/lib/chartDecisions';
import { TF_SECONDS, staleMessage } from '@/lib/liveCandle';
import { useLiveCandle } from './useLiveCandle';

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
  const [showQuiet, setShowQuiet] = useState(false);

  const configured = settings.data?.document.pairs.symbols;
  const symbols = useMemo(() => {
    const set = new Set<string>(configured ?? []);
    (positions.data ?? []).forEach((p) => set.add(p.symbol));
    return [...set];
  }, [configured, positions.data]);

  const symbol = chosenSymbol && symbols.includes(chosenSymbol) ? chosenSymbol : symbols[0];
  const defaultTf = settings.data?.document.pairs.timeframe ?? '1h';
  const timeframe = chosenTf ?? (isTimeframe(defaultTf) ? defaultTf : '1h');
  const candles = useCandles(symbol, timeframe, 300, showQuiet);
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

  const bars = useMemo(() => candles.data?.candles ?? [], [candles.data?.candles]);
  const { view, fresh } = useLiveCandle(symbol, symbols, timeframe, bars);
  const decisionGroups = useMemo(
    () =>
      groupDecisions(
        candles.data?.decisions ?? [],
        view.bars.map((k) => Math.floor(k.t / 1000)),
        TF_SECONDS[timeframe],
        showQuiet,
      ),
    [candles.data?.decisions, view.bars, timeframe, showQuiet],
  );
  const recentDecisions = useMemo(
    () =>
      (candles.data?.decisions ?? [])
        .filter((d) => showQuiet || d.tier !== 'quiet')
        .slice(-5)
        .reverse(),
    [candles.data?.decisions, showQuiet],
  );
  const last = bars[bars.length - 1];
  const summary = last
    ? `Gráfico de velas de ${symbol ?? ''}, ${bars.length} velas de ${timeframe}, último fechamento ${formatNumber(last.c)}${view.hasLive && view.livePrice !== null ? `, vela atual em formação a ${formatNumber(view.livePrice)}` : ''}. Os mesmos dados estão na tabela das últimas velas e as decisões estão na lista abaixo do gráfico.`
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
    view,
    stale: fresh.stale ? staleMessage(fresh.lastDataAt) : null,
    showQuiet,
    setShowQuiet,
    decisionGroups,
    recentDecisions,
    disconnected,
    summary,
  };
}
