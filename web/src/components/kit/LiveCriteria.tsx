import { CircleCheck, CircleX, Clock, type LucideIcon } from 'lucide-react';
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
    </ul>
  );
}
