import { ArrowDown, ArrowUp, ShieldCheck } from 'lucide-react';
import { ModeBadge } from '@/components/kit/ModeBadge';
import { StatusIndicator } from '@/components/kit/StatusIndicator';
import { StopAllButton } from '@/components/kit/StopAll';
import { Skeleton } from '@/components/ui/skeleton';
import type { StatusFace } from '@/lib/bot-state';
import { directionOf, formatNumber, formatPercent, formatSigned, spokenSigned } from '@/lib/format';
import { MODE_META, type TradingMode } from '@/lib/modes';
import { cn } from '@/lib/utils';

export interface TopBarViewProps {
  face: StatusFace;
  mode: TradingMode | null;
  balance: number | null;
  dayPnl: number | null;
  dayPnlPct: number | null;
  /** Painel sem conexao: valores esmaecidos e marcados como desatualizados. */
  stale?: boolean;
  operatorName?: string;
  canStop?: boolean;
  className?: string;
  /** No /_kit varias barras convivem na pagina: nao podem ser varios landmarks de banner. */
  as?: 'header' | 'div';
}

function Metric({
  label,
  children,
  sr,
  muted,
}: {
  label: string;
  children: React.ReactNode;
  sr?: string;
  muted?: boolean;
}) {
  return (
    <div className="flex flex-col leading-[1.2]">
      <span className="text-cap text-muted">{label}</span>
      <span className={cn('text-metric font-bold tabular-nums', muted && 'text-muted')}>
        {sr ? <span className="sr-only">{sr}</span> : null}
        <span aria-hidden={sr ? true : undefined}>{children}</span>
      </span>
    </div>
  );
}

export function TopBarView({
  face,
  mode,
  balance,
  dayPnl,
  dayPnlPct,
  stale,
  operatorName,
  canStop = true,
  className,
  as: Root = 'header',
}: TopBarViewProps) {
  const dir = dayPnl === null ? 'flat' : directionOf(dayPnl);
  const PnlIcon = dir === 'up' ? ArrowUp : ArrowDown;
  // Sem conexao os valores perdem a cor de ganho/perda e ficam neutros (esmaecidos sem quebrar o contraste AA).
  const pnlTone = stale
    ? 'text-muted'
    : dir === 'up'
      ? 'text-gain'
      : dir === 'down'
        ? 'text-loss'
        : 'text-text';

  return (
    <Root
      data-mode={mode ?? 'unknown'}
      className={cn(
        'flex flex-wrap items-center gap-2.5 border-b border-border bg-[color-mix(in_srgb,var(--bg)_92%,transparent)] px-4 py-3 backdrop-blur-sm sm:gap-4 sm:px-5 lg:px-7 lg:py-3.5',
        className,
      )}
    >
      <StatusIndicator {...face} />
      {mode ? (
        <ModeBadge mode={mode} hintOnMobile={false} />
      ) : (
        <Skeleton className="h-9 w-32 rounded-badge" />
      )}
      <div className="hidden flex-1 sm:block" />
      <div
        className={cn(
          'order-3 flex w-full items-center justify-between gap-3 border-t border-border pt-2.5 sm:order-none sm:w-auto sm:justify-start sm:gap-[22px] sm:border-0 sm:pt-0',
        )}
      >
        <Metric label={mode ? MODE_META[mode].money : 'Saldo'} muted={stale}>
          {mode === 'live' ? (
            <ShieldCheck className="mr-1 inline size-4 align-[-2px]" aria-hidden="true" />
          ) : null}
          {balance === null ? '—' : formatNumber(balance)}{' '}
          <small className="font-semibold text-muted">USDT</small>
        </Metric>
        <Metric
          label="Resultado do dia"
          muted={stale}
          sr={
            dayPnl === null || dayPnlPct === null
              ? 'sem dados'
              : `${spokenSigned(dayPnl)} USDT, ${spokenSigned(dayPnlPct)} por cento`
          }
        >
          {dayPnl === null || dayPnlPct === null ? (
            '—'
          ) : (
            <span className={cn('inline-flex items-center gap-1', pnlTone)}>
              {dir !== 'flat' ? <PnlIcon className="size-4" aria-hidden="true" /> : null}
              {formatSigned(dayPnl)}{' '}
              <small className="font-semibold">({formatPercent(dayPnlPct, 2, true)})</small>
            </span>
          )}
        </Metric>
        {stale ? (
          <span className="rounded-pill border border-dashed border-border-strong px-2 text-cap font-bold text-muted">
            desatualizado
          </span>
        ) : null}
      </div>
      <StopAllButton
        mode={mode}
        operatorName={operatorName}
        canStop={canStop}
        className="max-sm:order-2"
      />
    </Root>
  );
}
