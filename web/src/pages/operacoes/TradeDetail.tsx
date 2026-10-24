import { X } from 'lucide-react';
import { useMemo } from 'react';
import { useCandles } from '@/api/marketQueries';
import type { CandleMarker, Trade } from '@/api/marketSchemas';
import { TIMEFRAMES, type Timeframe } from '@/api/marketSchemas';
import { useSettings } from '@/api/queries';
import { CandleChart, type ChartLevel } from '@/components/kit/CandleChart';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { RESULT_LOOK } from './look';
import { Button } from '@/components/ui/button';
import { SheetClose, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
import { directionOf, formatNumber, formatSigned } from '@/lib/format';
import { cn } from '@/lib/utils';

const stamp = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso));

const isTimeframe = (v: string): v is Timeframe => (TIMEFRAMES as readonly string[]).includes(v);
const MARGIN_BEFORE = 36;
const MARGIN_AFTER = 12;

export function TradeDetail({ trade: t }: { trade: Trade }) {
  const settings = useSettings();
  const configured = settings.data?.document.pairs.timeframe ?? '1h';
  const timeframe: Timeframe = isTimeframe(configured) ? configured : '1h';
  const candles = useCandles(t.symbol, timeframe, 1000);
  const look = RESULT_LOOK[t.result];
  const dir = directionOf(t.pnl);

  const view = useMemo(() => {
    const all = candles.data?.candles ?? [];
    const opened = new Date(t.opened_at).getTime();
    const closed = new Date(t.closed_at).getTime();
    const from = all.findIndex((k) => new Date(k.t).getTime() >= opened);
    const lastIdx = all.findLastIndex((k) => new Date(k.t).getTime() <= closed);
    if (from < 0 || lastIdx < 0 || all.length === 0) return null;
    return all.slice(Math.max(0, from - MARGIN_BEFORE), lastIdx + 1 + MARGIN_AFTER);
  }, [candles.data, t.opened_at, t.closed_at]);

  const exitTitle =
    t.exit_reason === 'stop_loss' ? 'Stop' : t.exit_reason === 'take_profit' ? 'Alvo' : 'Saída';
  const exitKind: CandleMarker['kind'] =
    t.exit_reason === 'stop_loss' ? 'stop' : t.exit_reason === 'take_profit' ? 'target' : 'exit';
  const markers: CandleMarker[] = [
    { t: t.opened_at, kind: 'entry', price: t.entry_price, text: 'Entrada' },
    { t: t.closed_at, kind: exitKind, price: t.exit_price, text: t.exit_reason_text },
  ];
  const levels: ChartLevel[] = [
    { price: t.entry_price, title: 'Entrada', tone: 'entry' },
    {
      price: t.exit_price,
      title: exitTitle,
      tone:
        t.exit_reason === 'stop_loss'
          ? 'stop'
          : t.exit_reason === 'take_profit'
            ? 'target'
            : 'exit',
    },
  ];

  return (
    <SheetContent className="sm:max-w-[780px]" aria-describedby="trade-desc">
      <div className="flex items-start justify-between gap-3">
        <SheetTitle className="text-card font-bold">
          {t.symbol} · {stamp(t.opened_at)}
        </SheetTitle>
        <SheetClose asChild>
          <Button size="icon" variant="ghost" aria-label="Fechar detalhes">
            <X className="size-5" aria-hidden="true" />
          </Button>
        </SheetClose>
      </div>
      <SheetDescription id="trade-desc" className="sr-only">
        Detalhes da operação encerrada e o gráfico do momento.
      </SheetDescription>

      <div className="flex flex-wrap items-center gap-3">
        <Chip tone={look.tone} icon={look.icon}>
          {look.label}
        </Chip>
        <span
          className={cn(
            'text-kpi font-bold tabular-nums',
            dir === 'up' ? 'text-gain' : dir === 'down' ? 'text-loss' : 'text-muted',
          )}
        >
          {formatSigned(t.pnl)} USDT
        </span>
        {t.r_multiple !== null ? (
          <span className="font-semibold tabular-nums">
            {formatSigned(t.r_multiple, 1)} <GlossaryTerm term="r" />
          </span>
        ) : null}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sec sm:grid-cols-4">
        {[
          ['Entrada', formatNumber(t.entry_price)],
          ['Saída', formatNumber(t.exit_price)],
          ['Quantidade', formatNumber(t.quantity, 4)],
          ['Taxas', `${formatNumber(t.fees)} USDT`],
          ['Estratégia', t.strategy ?? '—'],
          ['Motivo da saída', t.exit_reason_text],
          ['Abertura', stamp(t.opened_at)],
          ['Encerramento', stamp(t.closed_at)],
        ].map(([k, v]) => (
          <div key={k}>
            <dt className="text-muted">{k}</dt>
            <dd className="font-semibold tabular-nums">{v}</dd>
          </div>
        ))}
      </dl>

      {candles.isError ? (
        <CardError error={candles.error} onRetry={() => void candles.refetch()} />
      ) : candles.isPending ? (
        <Skeleton className="h-[300px]" aria-busy="true" />
      ) : view ? (
        <>
          <CandleChart
            candles={view}
            markers={markers}
            levels={levels}
            showElephants={false}
            height={300}
            label={`Gráfico de ${t.symbol} no momento da operação: entrada em ${formatNumber(t.entry_price)} e saída em ${formatNumber(t.exit_price)} (${t.exit_reason_text}).`}
          />
          <p className="text-cap text-muted">
            Velas de {timeframe}. Linha cheia: entrada. Linha tracejada: saída ({exitTitle}).
          </p>
        </>
      ) : (
        <p className="rounded-xl border border-dashed border-border-strong p-4 text-sec text-muted">
          As velas desse momento não estão mais disponíveis, mas os números da operação acima
          continuam valendo.
        </p>
      )}
    </SheetContent>
  );
}
