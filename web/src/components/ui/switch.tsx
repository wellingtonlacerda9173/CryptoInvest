import { Switch as S } from 'radix-ui';
import type { ComponentProps } from 'react';
import { cn } from '@/lib/utils';

export function Switch({ className, ...props }: ComponentProps<typeof S.Root>) {
  return (
    <S.Root
      className={cn(
        'relative h-[30px] w-[52px] shrink-0 cursor-pointer rounded-pill border border-border-strong bg-surface-2 data-[state=checked]:border-gain data-[state=checked]:bg-gain',
        className,
      )}
      {...props}
    >
      <S.Thumb className="block size-[22px] translate-x-[3px] rounded-full bg-muted transition-transform duration-[180ms] ease-ui data-[state=checked]:translate-x-[25px] data-[state=checked]:bg-bg" />
    </S.Root>
  );
}
