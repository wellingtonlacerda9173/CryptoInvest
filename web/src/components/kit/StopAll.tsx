import { Square } from 'lucide-react';
import { createContext, use, useId, useRef, useState, type ReactNode, type RefObject } from 'react';
import { controlApi } from '@/api/queries';
import type { Command, StopAllRequest } from '@/api/schemas';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Field, Input } from '@/components/ui/input';
import { RadioCard, RadioGroup } from '@/components/ui/radio-group';
import { Tooltip } from '@/components/ui/tooltip';
import type { TradingMode } from '@/lib/modes';
import { commandFailureMessage } from '@/lib/commandFailure';
import { cn } from '@/lib/utils';
import { COMMAND_STATE_TEXT } from './CommandList';
import { ConfirmDialog } from './ConfirmDialog';
import { ModeBadge } from './ModeBadge';
import { notify } from './Toaster';

export type StopAllFn = (request: StopAllRequest) => Promise<Command | null | void>;

// Parar tudo = parada de emergencia: bloqueia novas operacoes e, se pedido, vende a mercado. Sem 2FA (reduz risco).
const defaultStopAll: StopAllFn = async (request) => {
  return controlApi.emergencyStop({
    liquidate: request.mode === 'liquidate',
    operator_name: request.operator || null,
    reason: request.reason || null,
  });
};

const StopAllContext = createContext<StopAllFn>(defaultStopAll);

export function StopAllProvider({
  stopAll,
  children,
}: {
  stopAll: StopAllFn;
  children: ReactNode;
}) {
  return <StopAllContext value={stopAll}>{children}</StopAllContext>;
}
export const useStopAll = () => use(StopAllContext);

type StopOption = StopAllRequest['mode'];

function StopAllDialog({
  open,
  onOpenChange,
  mode,
  defaultOperator,
  returnFocusRef,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: TradingMode | null;
  defaultOperator: string;
  returnFocusRef: RefObject<HTMLElement | null>;
}) {
  const stopAll = useStopAll();
  const uid = useId();
  const reasonRef = useRef<HTMLInputElement>(null);
  const [option, setOption] = useState<StopOption>('block_new');
  const [operator, setOperator] = useState(defaultOperator);
  const [reason, setReason] = useState('');
  const [ack, setAck] = useState(false);

  const liquidating = option === 'liquidate';
  // Modo ainda desconhecido: trata como dinheiro real (lado seguro).
  const real = mode === null || mode === 'live';
  const needsAck = liquidating && real;
  // Em emergencia, "so bloquear" nao exige nada (so reduz risco); vender exige nome e motivo.
  const invalid = liquidating && (!operator.trim() || !reason.trim() || (needsAck && !ack));

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      tone="danger"
      title="Parar tudo agora?"
      description={
        <p>
          {mode ? (
            <>
              Isto vale para o modo <ModeBadge mode={mode} className="min-h-6" />.{' '}
            </>
          ) : (
            'Isto vale para o modo atual do robô. '
          )}
          {real ? 'Isto vende em dinheiro real.' : 'Nenhum dinheiro real será mexido.'}
        </p>
      }
      confirmLabel="Confirmar: parar tudo"
      confirmDisabled={invalid}
      initialFocusRef={reasonRef}
      returnFocusRef={returnFocusRef}
      onConfirm={async () => {
        let command: Command | null | void;
        try {
          command = await stopAll({
            mode: option,
            operator: operator.trim(),
            reason: reason.trim(),
          });
        } catch (error) {
          const message = commandFailureMessage(error);
          notify.error('Parada não confirmada', message);
          throw new Error(message, { cause: error });
        }
        const state = command ? COMMAND_STATE_TEXT[command.state] : null;
        notify.success(
          state ? `Parada: ${state.label.toLowerCase()}` : 'Parada enviada ao robô',
          state ? state.help : 'Aguarde o robô confirmar no Histórico de controle.',
        );
      }}
    >
      <RadioGroup
        value={option}
        onValueChange={(v) => setOption(v as StopOption)}
        aria-label="O que fazer com o que está aberto"
        className="flex flex-col gap-3.5"
      >
        <RadioCard
          id={`${uid}-block`}
          value="block_new"
          title="Só bloquear novas operações"
          description="Mantém a posição aberta, com stop ativo. Recomendado."
        />
        <RadioCard
          id={`${uid}-sell`}
          value="liquidate"
          title="Bloquear e vender tudo a mercado"
          description="Fecha as posições abertas agora. Pode ter perda por preço pior."
        />
      </RadioGroup>
      <Field label="Seu nome (fica no registro)" htmlFor={`${uid}-op`}>
        <Input
          id={`${uid}-op`}
          value={operator}
          onChange={(e) => setOperator(e.target.value)}
          autoComplete="off"
          maxLength={64}
        />
      </Field>
      <Field
        label="Motivo"
        htmlFor={`${uid}-why`}
        help={
          liquidating ? 'Obrigatório para vender tudo.' : 'Opcional ao só bloquear novas operações.'
        }
        helpId={`${uid}-why-h`}
      >
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
      {needsAck ? (
        <label className="flex min-h-11 cursor-pointer items-center gap-2.5 text-[14px] font-semibold">
          <Checkbox checked={ack} onCheckedChange={(v) => setAck(v === true)} />
          Entendo que isto vende em dinheiro real.
        </label>
      ) : null}
    </ConfirmDialog>
  );
}

/** Sempre visivel na barra superior, igual no desktop e no celular, sem 2FA e sem atalho de teclado. */
export function StopAllButton({
  mode,
  operatorName = '',
  canStop = true,
  className,
  label = 'Parar tudo',
}: {
  mode: TradingMode | null;
  operatorName?: string;
  canStop?: boolean;
  className?: string;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  // Chave nova a cada abertura: o formulario sempre comeca limpo, sem desmontar (o foco volta ao botao).
  const [openCount, setOpenCount] = useState(0);
  const buttonRef = useRef<HTMLButtonElement>(null);

  const button = (
    <Button
      ref={buttonRef}
      variant="danger"
      aria-haspopup="dialog"
      aria-disabled={!canStop || undefined}
      className={cn('ml-auto max-sm:px-3', className)}
      onClick={() => {
        if (!canStop) return;
        setOpenCount((n) => n + 1);
        setOpen(true);
      }}
    >
      <Square className="size-4 fill-current" aria-hidden="true" />
      <span>{label}</span>
    </Button>
  );

  return (
    <>
      {canStop ? (
        button
      ) : (
        <Tooltip side="bottom" content="Somente administradores podem parar o robô.">
          {button}
        </Tooltip>
      )}
      <StopAllDialog
        key={openCount}
        open={open}
        onOpenChange={setOpen}
        mode={mode}
        defaultOperator={operatorName}
        returnFocusRef={buttonRef}
      />
    </>
  );
}
