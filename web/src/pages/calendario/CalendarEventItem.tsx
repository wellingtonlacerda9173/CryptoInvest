import { Pause } from 'lucide-react';
import type { CalendarEvent } from '@/api/calendarSchemas';
import { Chip } from '@/components/kit/Chip';
import {
  EVENT_COPY,
  eventName,
  isActive,
  localRange,
  relativeDay,
  utcRange,
  utcSpoken,
} from '@/lib/calendar';
import { cn } from '@/lib/utils';

export function CalendarEventItem({ event: e, nowMs }: { event: CalendarEvent; nowMs: number }) {
  const active = isActive(e, nowMs);
  const copy = EVENT_COPY[e.kind];
  return (
    <li
      data-active={active}
      className={cn(
        'flex min-w-0 flex-col gap-1 rounded-xl border px-3.5 py-3',
        active ? 'border-2 border-warn bg-warn-bg' : 'border-border bg-surface',
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <time dateTime={e.start} className="text-sec font-semibold tabular-nums">
          {localRange(e.start, e.end)}
        </time>
        <span className="flex flex-wrap items-center gap-1.5">
          {active ? (
            <Chip tone="warn" icon={Pause}>
              Robô parado agora
            </Chip>
          ) : null}
          <Chip tone="info" icon={false}>
            {copy.chip}
          </Chip>
        </span>
      </div>
      <strong className="break-words text-[16px]">{eventName(e)}</strong>
      <p className="break-words">{copy.why}</p>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-sec">
        <span className={active ? 'text-text' : 'text-muted'}>
          {active ? 'Em andamento' : relativeDay(Date.parse(e.start), nowMs)} · Fonte: {e.source}
        </span>
        <span className={cn('text-[12px] tabular-nums', active ? 'text-text' : 'text-muted')}>
          <span aria-hidden="true">{utcRange(e.start, e.end)}</span>
          <span className="sr-only">{utcSpoken(e.start, e.end)}</span>
        </span>
      </div>
    </li>
  );
}
