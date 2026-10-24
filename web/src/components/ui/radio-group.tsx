import { RadioGroup as R } from 'radix-ui';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export const RadioGroup = R.Root;

export function RadioCard({
  value,
  id,
  title,
  description,
  className,
}: {
  value: string;
  id: string;
  title: ReactNode;
  description?: ReactNode;
  className?: string;
}) {
  return (
    <label
      htmlFor={id}
      className={cn(
        'flex min-h-11 cursor-pointer items-start gap-2.5 rounded-xl border border-border-strong px-3 py-2.5 has-[[data-state=checked]]:border-info has-[[data-state=checked]]:bg-info-bg',
        className,
      )}
    >
      <R.Item
        value={value}
        id={id}
        className="mt-0.5 grid size-[18px] shrink-0 place-items-center rounded-full border-2 border-border-strong data-[state=checked]:border-info"
      >
        <R.Indicator className="size-2 rounded-full bg-info" />
      </R.Item>
      <span className="flex flex-col">
        <strong>{title}</strong>
        {description ? (
          <span className="text-sec font-normal text-muted">{description}</span>
        ) : null}
      </span>
    </label>
  );
}
