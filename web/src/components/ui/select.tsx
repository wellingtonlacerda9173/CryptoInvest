import { ChevronDown } from 'lucide-react';
import { useId, type ComponentProps } from 'react';
import { cn } from '@/lib/utils';

/** Select nativo (teclado e leitores de tela de graca), com o visual dos campos do kit. */
export function Select({ className, children, ...props }: ComponentProps<'select'>) {
  return (
    <span className="relative inline-flex">
      <select
        className={cn(
          'min-h-11 w-full cursor-pointer appearance-none rounded-control border border-border-strong bg-bg py-0 pl-3 pr-9 text-text disabled:cursor-not-allowed disabled:border-dashed disabled:bg-surface-2',
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-muted"
        aria-hidden="true"
      />
    </span>
  );
}

export function LabeledSelect({
  label,
  className,
  ...props
}: ComponentProps<'select'> & { label: string }) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1 text-cap font-semibold text-muted">
      <span id={id}>{label}</span>
      <Select aria-labelledby={id} className={className} {...props} />
    </div>
  );
}
