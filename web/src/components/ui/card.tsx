import type { ComponentProps } from 'react';
import { cn } from '@/lib/utils';

export function Card({ className, ...props }: ComponentProps<'section'>) {
  return (
    <section
      className={cn(
        'flex min-w-0 flex-col gap-3.5 rounded-card border border-border bg-surface p-4 shadow-card sm:p-5',
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      className={cn('flex flex-wrap items-center justify-between gap-3', className)}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }: ComponentProps<'h2'>) {
  return <h2 className={cn('flex items-center gap-2 text-card font-bold', className)} {...props} />;
}

export function CardHint({ className, ...props }: ComponentProps<'p'>) {
  return <p className={cn('text-sec text-muted', className)} {...props} />;
}
