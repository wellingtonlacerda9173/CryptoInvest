import { cva, type VariantProps } from 'class-variance-authority';
import { Slot } from 'radix-ui';
import type { ComponentProps } from 'react';
import { cn } from '@/lib/utils';

export const buttonVariants = cva(
  'inline-flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-control border px-4 font-semibold no-underline transition-colors duration-120 aria-disabled:cursor-not-allowed aria-disabled:border-dashed aria-disabled:border-border-strong aria-disabled:bg-surface-2 aria-disabled:text-muted disabled:cursor-not-allowed disabled:border-dashed disabled:border-border-strong disabled:bg-surface-2 disabled:text-muted',
  {
    variants: {
      variant: {
        default: 'border-border-strong bg-surface text-text hover:bg-surface-2',
        primary:
          'border-primary bg-primary text-primary-text hover:bg-[color-mix(in_srgb,var(--primary)_88%,#000)]',
        danger:
          'border-live-fill bg-live-fill text-live-text hover:bg-[color-mix(in_srgb,var(--live-fill)_86%,#000)]',
        ghost: 'border-border-strong bg-transparent text-text hover:bg-surface-2',
      },
      size: { default: '', icon: 'w-11 px-0' },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
);

export interface ButtonProps extends ComponentProps<'button'>, VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export function Button({
  className,
  variant,
  size,
  asChild,
  type = 'button',
  ...props
}: ButtonProps) {
  const Comp = asChild ? Slot.Root : 'button';
  return (
    <Comp
      className={cn(buttonVariants({ variant, size }), className)}
      {...(asChild ? {} : { type })}
      {...props}
    />
  );
}
