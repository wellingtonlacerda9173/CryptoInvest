import type { ReactNode } from 'react';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

export type KpiTone = 'neutral' | 'gain' | 'loss' | 'warn' | 'info';
const TONE: Record<KpiTone, string> = {
  neutral: 'text-muted',
  gain: 'text-gain',
  loss: 'text-loss',
  warn: 'text-warn',
  info: 'text-info',
};
const GLYPH: Partial<Record<KpiTone, string>> = { gain: '▲', loss: '▼' };

export interface KpiCardProps {
  label: ReactNode;
  value?: ReactNode;
  unit?: ReactNode;
  delta?: ReactNode;
  tone?: KpiTone;
  loading?: boolean;
  /** Mostra a seta ▲/▼ antes do delta (nunca so cor). */
  glyph?: boolean;
}

export function KpiCard({
  label,
  value,
  unit,
  delta,
  tone = 'neutral',
  loading,
  glyph = true,
}: KpiCardProps) {
  return (
    <div
      aria-busy={loading || undefined}
      data-tone={tone}
      className="flex min-w-0 flex-col gap-1.5 rounded-card border border-border bg-surface p-3.5 shadow-card sm:px-[18px] sm:py-4"
    >
      <span className="flex items-center gap-1.5 text-sec text-muted">{label}</span>
      {loading ? (
        <>
          <Skeleton className="h-[30px] w-3/4" />
          <Skeleton className="h-[18px] w-1/2" />
        </>
      ) : (
        <>
          <span className="min-w-0 text-[22px] font-bold leading-[1.1] tracking-[-0.02em] tabular-nums sm:text-kpi">
            {value}
            {unit ? (
              <small className="whitespace-nowrap text-[14px] font-semibold tracking-normal text-muted max-sm:block">
                {' '}
                {unit}
              </small>
            ) : null}
          </span>
          {delta ? (
            <span
              className={cn(
                'inline-flex items-center gap-1 text-sec font-semibold tabular-nums',
                TONE[tone],
              )}
            >
              {glyph && GLYPH[tone] ? <span aria-hidden="true">{GLYPH[tone]}</span> : null}
              {delta}
            </span>
          ) : null}
        </>
      )}
    </div>
  );
}
