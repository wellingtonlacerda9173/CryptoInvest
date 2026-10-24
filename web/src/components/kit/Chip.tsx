import { CircleCheck, CircleX, Clock, Info, TriangleAlert, type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export type ChipTone = 'ok' | 'warn' | 'bad' | 'off' | 'info';

const STYLE: Record<ChipTone, { box: string; Icon: LucideIcon }> = {
  ok: { box: 'border-gain bg-gain-bg text-gain', Icon: CircleCheck },
  warn: { box: 'border-warn bg-warn-bg text-warn', Icon: TriangleAlert },
  bad: { box: 'border-loss bg-loss-bg text-loss', Icon: CircleX },
  off: { box: 'border-border-strong bg-surface-2 text-neutral', Icon: Clock },
  info: { box: 'border-info bg-info-bg text-info', Icon: Info },
};

/** Estado em palavra + icone + cor (nunca so cor). */
export function Chip({
  tone,
  icon,
  children,
  className,
}: {
  tone: ChipTone;
  icon?: LucideIcon | false;
  children: ReactNode;
  className?: string;
}) {
  const { box, Icon } = STYLE[tone];
  const Glyph = icon === false ? null : (icon ?? Icon);
  return (
    <span
      data-tone={tone}
      className={cn(
        'inline-flex min-h-[26px] items-center gap-1.5 whitespace-nowrap rounded-pill border px-2.5 text-cap font-bold',
        box,
        className,
      )}
    >
      {Glyph ? <Glyph className="size-4" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}
