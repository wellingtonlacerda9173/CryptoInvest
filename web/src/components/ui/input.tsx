import type { ComponentProps, ReactNode } from 'react';
import { cn } from '@/lib/utils';

export function Input({ className, ...props }: ComponentProps<'input'>) {
  return (
    <input
      className={cn(
        'min-h-11 w-full rounded-control border border-border-strong bg-bg px-3 text-text placeholder:text-muted disabled:cursor-not-allowed disabled:border-dashed disabled:bg-surface-2 aria-[invalid=true]:border-2 aria-[invalid=true]:border-loss',
        className,
      )}
      {...props}
    />
  );
}

export function Field({
  label,
  htmlFor,
  help,
  helpId,
  error,
  errorId,
  children,
}: {
  label: ReactNode;
  htmlFor: string;
  help?: ReactNode;
  helpId?: string;
  error?: ReactNode;
  errorId?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-[14px] font-semibold">
        {label}
      </label>
      {children}
      {error ? (
        <span id={errorId} className="flex items-center gap-1.5 text-cap text-loss">
          {error}
        </span>
      ) : help ? (
        <span id={helpId} className="text-cap text-muted">
          {help}
        </span>
      ) : null}
    </div>
  );
}
