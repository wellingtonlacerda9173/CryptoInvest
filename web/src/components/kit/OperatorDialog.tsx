import { useId, useRef, useState, type ReactNode, type RefObject } from 'react';
import { Field, Input } from '@/components/ui/input';
import { ConfirmDialog } from './ConfirmDialog';

export interface OperatorInput {
  operator: string;
  reason: string;
}

/** Pede nome e motivo (ficam no registro) antes de uma acao no robo. O formulario sempre abre limpo. */
export function OperatorDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  tone = 'primary',
  defaultOperator,
  reasonHelp = 'Obrigatório. Fica no histórico.',
  returnFocusRef,
  onSubmit,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  tone?: 'primary' | 'danger';
  defaultOperator: string;
  reasonHelp?: string;
  returnFocusRef?: RefObject<HTMLElement | null>;
  onSubmit: (input: OperatorInput) => Promise<void>;
  children?: ReactNode;
}) {
  const uid = useId();
  const reasonRef = useRef<HTMLInputElement>(null);
  const [operator, setOperator] = useState(defaultOperator);
  const [reason, setReason] = useState('');

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setReason('');
        onOpenChange(next);
      }}
      title={title}
      description={description}
      confirmLabel={confirmLabel}
      tone={tone}
      confirmDisabled={!operator.trim() || !reason.trim()}
      initialFocusRef={reasonRef}
      returnFocusRef={returnFocusRef}
      onConfirm={() => onSubmit({ operator: operator.trim(), reason: reason.trim() })}
    >
      {children}
      <Field label="Seu nome (fica no registro)" htmlFor={`${uid}-op`}>
        <Input
          id={`${uid}-op`}
          value={operator}
          onChange={(e) => setOperator(e.target.value)}
          autoComplete="off"
          maxLength={64}
        />
      </Field>
      <Field label="Motivo" htmlFor={`${uid}-why`} help={reasonHelp} helpId={`${uid}-why-h`}>
        <Input
          id={`${uid}-why`}
          ref={reasonRef}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          autoComplete="off"
          maxLength={280}
          aria-describedby={`${uid}-why-h`}
        />
      </Field>
    </ConfirmDialog>
  );
}
