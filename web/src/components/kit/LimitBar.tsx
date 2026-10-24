import { CircleCheck, CircleX, TriangleAlert, type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export type LimitLevel = 'ok' | 'warn' | 'bad';

/** Ate 60% folgado, 60-90% atencao, a partir de 90% no limite (design-system.md, 6). */
export const levelFor = (ratio: number): LimitLevel =>
  ratio >= 0.9 ? 'bad' : ratio >= 0.6 ? 'warn' : 'ok';

const STYLE: Record<LimitLevel, { text: string; fill: string; Icon: LucideIcon; word: string }> = {
  ok: { text: 'text-gain', fill: 'bg-gain', Icon: CircleCheck, word: 'Folgado' },
  warn: { text: 'text-warn', fill: 'bg-warn', Icon: TriangleAlert, word: 'Atenção' },
  bad: { text: 'text-loss', fill: 'bg-loss', Icon: CircleX, word: 'No limite' },
};

export interface LimitBarProps {
  name: ReactNode;
  /** Nome em texto puro para leitores de tela. */
  label: string;
  /** Valor ja formatado, ex.: "0,3% de 1%". */
  valueText: string;
  value: number;
  limit: number;
  note?: ReactNode;
  level?: LimitLevel;
  /** Palavra de estado quando o nivel e "ok" (ex.: "Normal") ou texto completo do estado. */
  stateText?: string;
}

export function LimitBar({
  name,
  label,
  valueText,
  value,
  limit,
  note,
  level,
  stateText,
}: LimitBarProps) {
  const ratio = limit > 0 ? value / limit : 0;
  const lvl = level ?? levelFor(ratio);
  const { text, fill, Icon, word } = STYLE[lvl];
  const stateLabel = stateText ?? word;
  const pct = Math.round(Math.min(1, Math.max(0, ratio)) * 100);

  return (
    <div role="group" aria-label={`${label}: ${valueText}. ${stateLabel}`} data-level={lvl}>
      <div className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between sm:gap-2">
        <span className="font-semibold">{name}</span>
        <span className="font-semibold tabular-nums sm:whitespace-nowrap">{valueText}</span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        className="relative mt-1.5 h-2.5 overflow-hidden rounded-pill border border-border-strong bg-surface-2"
      >
        <div
          className={cn('h-full rounded-pill transition-[width] duration-[400ms] ease-out', fill)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-1 flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between sm:gap-2">
        <span className={cn('inline-flex items-center gap-1 text-cap font-bold', text)}>
          <Icon className="size-4" aria-hidden="true" />
          {stateLabel}
        </span>
        {note ? <span className="text-cap text-muted">{note}</span> : null}
      </div>
    </div>
  );
}
