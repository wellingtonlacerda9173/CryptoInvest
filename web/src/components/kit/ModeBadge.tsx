import { CircleDollarSign, Clock, ShieldCheck, type LucideIcon } from 'lucide-react';
import { MODE_META, type TradingMode } from '@/lib/modes';
import { cn } from '@/lib/utils';

const ICON: Record<TradingMode, LucideIcon> = {
  backtest: Clock,
  paper: CircleDollarSign,
  live: ShieldCheck,
};

// Tres sinais redundantes: cor + forma da borda (pontilhada, tracejada, solida) + palavra.
const STYLE: Record<TradingMode, string> = {
  backtest: 'border-dotted border-backtest bg-backtest-bg text-backtest',
  paper: 'border-dashed border-info bg-info-bg text-info',
  live: 'border-solid border-live-fill bg-live-fill text-live-text shadow-[0_0_0_3px_color-mix(in_srgb,var(--live-fill)_30%,transparent)]',
};

export function ModeBadge({
  mode,
  className,
  hintOnMobile = true,
}: {
  mode: TradingMode;
  className?: string;
  /** Na barra superior do celular o subtexto some visualmente, mas continua para leitores de tela. */
  hintOnMobile?: boolean;
}) {
  const { label, hint } = MODE_META[mode];
  const Icon = ICON[mode];
  return (
    <span
      data-mode={mode}
      className={cn(
        'inline-flex min-h-9 items-center gap-2 whitespace-nowrap rounded-badge border-2 px-3 text-sec font-extrabold tracking-[0.06em]',
        STYLE[mode],
        className,
      )}
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" strokeWidth={2} />
      <span>{label}</span>
      <small
        className={cn('text-cap font-medium tracking-normal', !hintOnMobile && 'max-sm:sr-only')}
      >
        {hint}
      </small>
    </span>
  );
}
