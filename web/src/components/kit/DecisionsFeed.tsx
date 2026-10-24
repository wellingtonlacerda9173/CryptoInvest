import { ArrowUp, CircleDollarSign, Pause, type LucideIcon } from 'lucide-react';
import type { Decision } from '@/api/schemas';
import { Chip, type ChipTone } from './Chip';

const KIND: Record<Decision['outcome_kind'], { tone: ChipTone; icon: LucideIcon }> = {
  entered: { tone: 'ok', icon: ArrowUp },
  waited: { tone: 'off', icon: CircleDollarSign },
  refused: { tone: 'warn', icon: Pause },
  info: { tone: 'info', icon: CircleDollarSign },
};

const clock = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', { hour: '2-digit', minute: '2-digit' }).format(new Date(iso));

export function DecisionsFeed({ items }: { items: Decision[] }) {
  return (
    <ul className="flex flex-col">
      {items.map((d) => {
        const { tone, icon } = KIND[d.outcome_kind];
        return (
          <li
            key={d.id}
            className="grid grid-cols-[52px_1fr] gap-3 border-b border-border py-3 first:pt-0 last:border-0 last:pb-0"
          >
            <time dateTime={d.at} className="text-sec tabular-nums text-muted">
              {clock(d.at)}
            </time>
            <div>
              <div className="flex flex-wrap items-center gap-2 font-semibold">
                {d.symbol}
                <Chip tone={tone} icon={icon}>
                  {d.outcome}
                </Chip>
              </div>
              <p className="mt-0.5 text-sec text-muted">
                {d.reason_text}
                {d.code ? (
                  <>
                    {' '}
                    <code className="rounded bg-surface-2 px-1 text-cap">{d.code}</code>
                  </>
                ) : null}
              </p>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
