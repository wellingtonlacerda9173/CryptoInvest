import { AlertDialog as Alert, Dialog as Sheetish } from 'radix-ui';
import type { ComponentProps } from 'react';
import { cn } from '@/lib/utils';

const overlay = 'anim-fade fixed inset-0 z-[80] bg-black/60';
// Celular: folha que sobe de baixo (Sheet). Tablet/desktop: dialogo centralizado.
const panel =
  'anim-pop fixed inset-x-0 bottom-0 z-[81] flex max-h-[92dvh] flex-col gap-3.5 overflow-y-auto rounded-t-card border-2 bg-surface p-5 shadow-pop sm:inset-x-auto sm:bottom-auto sm:left-1/2 sm:top-1/2 sm:w-[calc(100%-32px)] sm:max-w-[520px] sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-card sm:p-[22px]';

export const AlertDialog = Alert.Root;
export const AlertDialogTrigger = Alert.Trigger;
export const AlertDialogCancel = Alert.Cancel;
export const AlertDialogAction = Alert.Action;
export const AlertDialogTitle = Alert.Title;
export const AlertDialogDescription = Alert.Description;

export function AlertDialogContent({
  className,
  tone = 'neutral',
  children,
  ...props
}: ComponentProps<typeof Alert.Content> & { tone?: 'danger' | 'neutral' }) {
  return (
    <Alert.Portal>
      <Alert.Overlay className={overlay} />
      <Alert.Content
        className={cn(panel, tone === 'danger' ? 'border-loss' : 'border-border-strong', className)}
        {...props}
      >
        {children}
      </Alert.Content>
    </Alert.Portal>
  );
}

export const Sheet = Sheetish.Root;
export const SheetTrigger = Sheetish.Trigger;
export const SheetClose = Sheetish.Close;
export const SheetTitle = Sheetish.Title;
export const SheetDescription = Sheetish.Description;

export function SheetContent({
  className,
  children,
  ...props
}: ComponentProps<typeof Sheetish.Content>) {
  return (
    <Sheetish.Portal>
      <Sheetish.Overlay className={overlay} />
      <Sheetish.Content className={cn(panel, 'border-border-strong', className)} {...props}>
        {children}
      </Sheetish.Content>
    </Sheetish.Portal>
  );
}
