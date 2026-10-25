import { CircleCheck, CircleX, Clock, TriangleAlert, type LucideIcon } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useCalendar } from '@/api/calendarQueries';
import type { LiveGate } from '@/api/schemas';
import { cn } from '@/lib/utils';

const STATUS: Record<
  LiveGate['criteria'][number]['status'],
  { Icon: LucideIcon; icon: string; value: string; word: string }
> = {
  met: { Icon: CircleCheck, icon: 'text-gain', value: 'text-gain', word: 'Cumprido' },
  failed: { Icon: CircleX, icon: 'text-loss', value: 'text-loss', word: 'Reprovado' },
  in_progress: { Icon: Clock, icon: 'text-neutral', value: 'text-text', word: 'Em andamento' },
};

/** Valor atual x meta, com palavra e icone para cada criterio (cumprido, reprovado, em andamento). */
function CalendarRow() {
  const state = useCalendar().data?.state;
  if (!state) return null;
  const view = {
    ok: { Icon: CircleCheck, icon: 'text-gain', word: 'Em dia', note: null },
    expiring: {
      Icon: TriangleAlert,
      icon: 'text-warn',
      word: 'Atenção',
      note: 'O calendário vence em breve.',
    },
    expired: {
      Icon: CircleX,
      icon: 'text-loss',
      word: 'Vencido',
      note: 'Calendário vencido: o robô não opera até ele ser atualizado.',
    },
  }[state];
  return (
    <li className="flex flex-wrap items-start gap-x-2.5 gap-y-1">
      <view.Icon className={cn('mt-0.5 size-5 shrink-0', view.icon)} aria-hidden="true" />
      <span className="min-w-0 flex-1 basis-[11rem]">
        Calendário de eventos em dia
        <span className="sr-only">: {view.word}.</span>
        {view.note ? <span className="block text-cap text-muted">{view.note}</span> : null}
      </span>
      <Link
        to="/calendario"
        aria-label="Ver o calendário de eventos"
        className="font-semibold underline underline-offset-2 max-sm:basis-full max-sm:pl-[30px] sm:ml-auto"
      >
        Ver calendário
      </Link>
    </li>
  );
}

export function LiveCriteria({ criteria }: { criteria: LiveGate['criteria'] }) {
  return (
    <ul aria-label="Critérios para liberar o Live" className="flex flex-col gap-2.5">
      {criteria.map((c) => {
        const { Icon, icon, value, word } = STATUS[c.status];
        return (
          <li key={c.code} className="flex flex-wrap items-start gap-x-2.5 gap-y-1">
            <Icon className={cn('mt-0.5 size-5 shrink-0', icon)} aria-hidden="true" />
            <span className="min-w-0 flex-1 basis-[11rem]">
              {c.name}
              <span className="sr-only">: {word}.</span>
              {c.status !== 'met' && (c.missing_text ?? c.reason) ? (
                <span className="block text-cap text-muted">{c.missing_text ?? c.reason}</span>
              ) : null}
            </span>
            <span
              className={cn(
                'font-bold tabular-nums max-sm:basis-full max-sm:pl-[30px] sm:ml-auto sm:text-right',
                value,
              )}
            >
              <span className="whitespace-nowrap">{c.value_text}</span>
              <span className="font-medium text-muted"> · meta {c.target_text}</span>
            </span>
          </li>
        );
      })}
      <CalendarRow />
    </ul>
  );
}
