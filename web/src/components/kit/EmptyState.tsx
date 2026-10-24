import type { LucideIcon } from 'lucide-react';
import { CircleDollarSign } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export function EmptyState({
  icon: Icon = CircleDollarSign,
  title,
  children,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'flex flex-col items-center gap-2 rounded-[14px] border border-dashed border-border-strong px-4 py-6 text-center text-muted',
        className,
      )}
    >
      <Icon className="size-8 text-info" aria-hidden="true" />
      <strong className="text-[16px] text-text">{title}</strong>
      {children ? <span>{children}</span> : null}
      {action}
    </div>
  );
}
