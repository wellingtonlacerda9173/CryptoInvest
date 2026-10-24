import type { StatusFace, StatusTone } from '@/lib/bot-state';
import { cn } from '@/lib/utils';

const TONE: Record<StatusTone, string> = {
  ok: 'border-gain bg-gain-bg text-gain',
  warn: 'border-warn bg-warn-bg text-warn',
  bad: 'border-loss bg-loss-bg text-loss',
  off: 'border-border-strong bg-surface-2 text-neutral',
  offline: 'border-dashed border-border-strong bg-surface-2 text-neutral',
};

// O pulso e o unico movimento continuo da interface; palavra e cor continuam sem animacao.
const PULSE: Partial<Record<StatusTone, string>> = {
  ok: 'status-pulse-ok',
  warn: 'status-pulse-warn',
};

export function StatusIndicator({
  tone,
  label,
  reason,
  className,
}: StatusFace & { className?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      data-tone={tone}
      className={cn(
        'inline-flex min-h-11 items-center gap-2 sm:gap-2.5 whitespace-nowrap rounded-pill border py-0 pl-2.5 pr-3 font-bold sm:pl-3.5 sm:pr-4',
        TONE[tone],
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn('relative size-3 shrink-0 rounded-full bg-current', PULSE[tone])}
      />
      <span>{label}</span>
      <span className="hidden text-sec font-medium text-muted sm:inline">{reason}</span>
      <span className="sr-only sm:hidden">, {reason}</span>
    </div>
  );
}
