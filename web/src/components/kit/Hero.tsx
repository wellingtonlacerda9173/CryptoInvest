import { CircleCheck, CircleX, PowerOff, TriangleAlert, type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export type HeroTone = 'ok' | 'warn' | 'bad' | 'off';

const STYLE: Record<HeroTone, { box: string; icon: string; Icon: LucideIcon }> = {
  ok: { box: 'border-gain bg-gain-bg', icon: 'border-gain text-gain', Icon: CircleCheck },
  warn: { box: 'border-warn bg-warn-bg', icon: 'border-warn text-warn', Icon: TriangleAlert },
  bad: { box: 'border-loss bg-loss-bg', icon: 'border-loss text-loss', Icon: CircleX },
  off: {
    box: 'border-border-strong bg-surface-2',
    icon: 'border-border-strong text-neutral',
    Icon: PowerOff,
  },
};

export function Hero({
  tone,
  title,
  children,
  headingId,
}: {
  tone: HeroTone;
  title: string;
  children?: ReactNode;
  headingId: string;
}) {
  const { box, icon, Icon } = STYLE[tone];
  return (
    <section
      aria-labelledby={headingId}
      data-tone={tone}
      className={cn(
        'grid grid-cols-1 items-center gap-[18px] rounded-card border p-4 sm:grid-cols-[auto_1fr] sm:px-6 sm:py-5',
        box,
      )}
    >
      <div
        className={cn(
          'grid size-11 place-items-center rounded-[14px] border bg-surface sm:size-[52px]',
          icon,
        )}
      >
        <Icon className="size-7" aria-hidden="true" />
      </div>
      <div>
        <h2 id={headingId} className="text-[18px] font-bold tracking-[-0.01em] sm:text-[20px]">
          {title}
        </h2>
        {children ? <p className="mt-0.5">{children}</p> : null}
      </div>
    </section>
  );
}
