import { useEffect, useState } from 'react';
import { useTicker } from '@/api/marketQueries';
import { directionOf, formatNumber, formatPercent } from '@/lib/format';
import { priceDigits, resolveLiveState, tickerSymbols, type LiveState } from '@/lib/ticker';
import { cn } from '@/lib/utils';

const STATE_TEXT: Record<LiveState, string> = {
  live: 'Cotação ao vivo',
  delayed: 'Cotação atrasada',
  none: 'Sem cotação ao vivo',
};

function useNow(intervalMs: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

function LiveBadge({ state }: { state: LiveState }) {
  const tone =
    state === 'live'
      ? 'border-gain bg-gain-bg text-gain'
      : state === 'delayed'
        ? 'border-warn bg-warn-bg text-warn'
        : 'border-dashed border-border-strong bg-surface-2 text-neutral';
  const pulse =
    state === 'live' ? 'status-pulse-ok' : state === 'delayed' ? 'status-pulse-warn' : '';
  return (
    <span
      data-state={state}
      aria-hidden="true"
      className={`text-cap ${cn(
        'inline-flex min-h-[26px] items-center gap-1.5 whitespace-nowrap rounded-pill border px-2.5 font-bold',
        tone,
      )}`}
    >
      <span
        aria-hidden="true"
        className={cn('relative size-2.5 shrink-0 rounded-full bg-current', pulse)}
      />
      {state === 'live' ? 'Ao vivo' : state === 'delayed' ? 'Atrasado' : 'Sem cotação ao vivo'}
    </span>
  );
}

/**
 * Preco grande do par escolhido, andando em tempo (quase) real, mais os outros pares em tira.
 * So exibicao: o robo nunca le este valor. Sem cotacao recente, cai para o ultimo preco do grafico, avisando.
 */
export function LivePrice({
  symbol,
  symbols,
  fallbackPrice,
  fallbackChangePct,
}: {
  symbol: string | undefined;
  symbols: readonly string[];
  fallbackPrice: number | null;
  fallbackChangePct: number | null;
}) {
  const ticker = useTicker(symbols, symbol);
  const now = useNow(1_000);
  const quotes = ticker.data?.quotes ?? [];
  const stateOf = (s: string | undefined) => {
    const q = quotes.find((x) => x.symbol === s);
    return [resolveLiveState(q, ticker.dataUpdatedAt, now), q] as const;
  };
  const [state, quote] = stateOf(symbol);
  const live = state !== 'none' && quote;
  const price = live ? quote.price : fallbackPrice;
  const change = live ? quote.change_24h_pct : fallbackChangePct;
  const dir = directionOf(change ?? 0);
  const others = tickerSymbols(symbols, symbol).filter((s) => s !== symbol);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
        {/* Cor em span interno: no mesmo cn(), `text-muted` engolia `text-kpi` (tailwind-merge) e o preço encolhia. */}
        <span className="text-kpi font-bold tabular-nums" data-testid="preco-grande">
          <span className={state === 'delayed' ? 'text-muted' : undefined}>
            {price !== null ? formatNumber(price, priceDigits(price)) : '—'}
          </span>
        </span>
        {change !== null ? (
          <span
            className={cn(
              'font-semibold tabular-nums',
              dir === 'up' ? 'text-gain' : dir === 'down' ? 'text-loss' : 'text-muted',
            )}
          >
            <span aria-hidden="true">{dir === 'up' ? '▲ ' : dir === 'down' ? '▼ ' : ''}</span>
            {formatPercent(change, 2, true)} em 24 h
          </span>
        ) : null}
        <LiveBadge state={state} />
        <span role="status" aria-live="polite" className="sr-only">
          {STATE_TEXT[state]}
        </span>
        {state === 'none' && price !== null ? (
          <span className="text-sec text-muted">Mostrando o último preço do gráfico.</span>
        ) : null}
      </div>
      {others.length > 0 ? (
        <ul
          className="flex flex-wrap gap-x-5 gap-y-1 text-sec text-muted"
          aria-label="Outros pares"
        >
          {others.map((s) => {
            const [st, q] = stateOf(s);
            return (
              <li key={s} className="tabular-nums">
                {s}{' '}
                {q && st !== 'none' ? (
                  <span className={cn('font-semibold', st === 'live' ? 'text-text' : 'text-muted')}>
                    {formatNumber(q.price, priceDigits(q.price))}
                    {st === 'delayed' ? ' (atrasado)' : ''}
                  </span>
                ) : (
                  <span>sem cotação ao vivo</span>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
