import { TriangleAlert, XCircle } from 'lucide-react';
import { useState, type FormEvent, type ReactNode, type RefObject } from 'react';
import { Button } from '@/components/ui/button';
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: 'danger' | 'primary';
  /** Nada acontece sem um clique em confirmar (ou Enter com o formulario valido). */
  onConfirm: () => void | Promise<void>;
  confirmDisabled?: boolean;
  /** Foco inicial; por padrao o botao de voltar (acao segura). */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Elemento que recebe o foco ao fechar (o dialogo e controlado, sem gatilho interno). */
  returnFocusRef?: RefObject<HTMLElement | null>;
  children?: ReactNode;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  cancelLabel = 'Voltar (Esc)',
  tone = 'primary',
  onConfirm,
  confirmDisabled,
  initialFocusRef,
  returnFocusRef,
  children,
}: ConfirmDialogProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleOpenChange = (next: boolean) => {
    if (pending) return;
    if (!next) setError(null);
    onOpenChange(next);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (confirmDisabled || pending) return;
    setPending(true);
    setError(null);
    try {
      await onConfirm();
      onOpenChange(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Não foi possível concluir. Nada mudou.');
    } finally {
      setPending(false);
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={handleOpenChange}>
      <AlertDialogContent
        tone={tone === 'danger' ? 'danger' : 'neutral'}
        onCloseAutoFocus={(e) => {
          e.preventDefault();
          returnFocusRef?.current?.focus();
        }}
        onOpenAutoFocus={
          initialFocusRef
            ? (e) => {
                e.preventDefault();
                initialFocusRef.current?.focus();
              }
            : undefined
        }
      >
        <form onSubmit={submit} className="flex flex-col gap-3.5" noValidate>
          <AlertDialogTitle
            className={cn(
              'flex items-center gap-2.5 text-[18px] font-bold',
              tone === 'danger' && 'text-loss',
            )}
          >
            <TriangleAlert className="size-5 shrink-0" aria-hidden="true" />
            {title}
          </AlertDialogTitle>
          {description ? (
            <AlertDialogDescription asChild>
              <div className="text-muted">{description}</div>
            </AlertDialogDescription>
          ) : null}
          {children}
          {error ? (
            <p role="alert" className="flex items-center gap-1.5 text-cap text-loss">
              <XCircle className="size-4 shrink-0" aria-hidden="true" />
              {error}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2.5">
            <Button
              type="submit"
              variant={tone === 'danger' ? 'danger' : 'primary'}
              className="flex-1"
              disabled={confirmDisabled || pending}
            >
              {confirmLabel}
            </Button>
            <AlertDialogCancel asChild>
              <Button variant="default" disabled={pending}>
                {cancelLabel}
              </Button>
            </AlertDialogCancel>
          </div>
        </form>
      </AlertDialogContent>
    </AlertDialog>
  );
}
