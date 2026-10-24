import { Tooltip as T } from 'radix-ui';
import type { ComponentProps, ReactElement, ReactNode } from 'react';

export const TooltipProvider = ({ children }: { children: ReactNode }) => (
  <T.Provider delayDuration={200} skipDelayDuration={300}>
    {children}
  </T.Provider>
);

export function Tooltip({
  content,
  children,
  side = 'right',
}: {
  content: ReactNode;
  children: ReactElement;
  side?: ComponentProps<typeof T.Content>['side'];
}) {
  return (
    <T.Root>
      <T.Trigger asChild>{children}</T.Trigger>
      <T.Portal>
        <T.Content
          side={side}
          sideOffset={8}
          className="anim-pop z-[85] max-w-[340px] rounded-control border border-border-strong bg-surface-2 px-3 py-2.5 text-sec text-text shadow-pop"
        >
          {content}
        </T.Content>
      </T.Portal>
    </T.Root>
  );
}
