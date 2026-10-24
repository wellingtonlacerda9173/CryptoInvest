import type { Command, CommandState } from '@/api/schemas';
import { Chip, type ChipTone } from './Chip';

/** Estado de entrega em texto simples: o que o robo ja fez com o comando (nunca so cor). */
export const COMMAND_STATE_TEXT: Record<
  CommandState,
  { label: string; tone: ChipTone; help: string }
> = {
  pending_delivery: {
    label: 'Aguardando entrega',
    tone: 'warn',
    help: 'O robô ainda não recebeu. Fica na fila até ele dar sinal de vida.',
  },
  delivered: { label: 'Entregue', tone: 'info', help: 'O robô recebeu e vai aplicar.' },
  applied: { label: 'Aplicado', tone: 'ok', help: 'O robô aplicou.' },
  partially_applied: {
    label: 'Aplicado em parte',
    tone: 'warn',
    help: 'O robô aplicou uma parte; o resto fica para o próximo início.',
  },
  refused: { label: 'Recusado', tone: 'bad', help: 'O robô recusou este comando.' },
  expired: { label: 'Expirado', tone: 'off', help: 'Passou do prazo sem o robô aplicar.' },
  discarded: { label: 'Descartado', tone: 'off', help: 'O robô descartou por segurança.' },
  failed: { label: 'Falhou', tone: 'bad', help: 'Não foi possível aplicar.' },
};

export const COMMAND_NAME_TEXT: Record<Command['name'], string> = {
  pause: 'Pausar novas entradas',
  resume: 'Retomar',
  stop: 'Parada ordenada',
  'emergency-stop': 'Parar tudo',
  reactivate: 'Reativar após desligamento',
  'apply-config': 'Aplicar configuração',
};

const when = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso));

export function CommandList({ items }: { items: Command[] }) {
  return (
    <ol className="flex flex-col gap-3">
      {items.map((c) => {
        const s = COMMAND_STATE_TEXT[c.state];
        return (
          <li
            key={c.id}
            data-state={c.state}
            className="flex flex-col gap-1 border-l-[3px] border-border-strong pl-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <time dateTime={c.created_at} className="text-sec tabular-nums text-muted">
                {when(c.created_at)}
              </time>
              <strong>{COMMAND_NAME_TEXT[c.name]}</strong>
              {c.liquidate ? <span className="text-sec text-loss">(vendendo tudo)</span> : null}
              <Chip tone={s.tone}>{s.label}</Chip>
            </div>
            <p className="text-sec text-muted">
              {c.operator}
              {c.reason ? `: ${c.reason}` : ''}. {c.detail ?? s.help}
            </p>
          </li>
        );
      })}
    </ol>
  );
}
