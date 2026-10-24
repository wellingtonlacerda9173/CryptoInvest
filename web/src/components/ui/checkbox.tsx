import { Check } from 'lucide-react';
import { Checkbox as C } from 'radix-ui';
import type { ComponentProps } from 'react';
import { cn } from '@/lib/utils';

export function Checkbox({ className, ...props }: ComponentProps<typeof C.Root>) {
  return (
    <C.Root
      className={cn(
        'grid size-[22px] shrink-0 place-items-center rounded-md border-2 border-border-strong bg-bg data-[state=checked]:border-info data-[state=checked]:bg-info-bg',
        className,
      )}
      {...props}
    >
      <C.Indicator>
        <Check className="size-4 text-info" aria-hidden="true" strokeWidth={3} />
      </C.Indicator>
    </C.Root>
  );
}
