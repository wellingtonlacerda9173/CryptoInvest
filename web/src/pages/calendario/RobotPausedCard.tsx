import { Pause, Play } from 'lucide-react';
import type { CalendarEvent } from '@/api/calendarSchemas';
import { brasiliaClock, eventName, lowerFirst, relativeDay } from '@/lib/calendar';
import { cn } from '@/lib/utils';

type Props = {
  expired: boolean;
  active: CalendarEvent | undefined;
  next: CalendarEvent | undefined;
  nowMs: number;
};

export function RobotPausedCard({ expired, active, next, nowMs }: Props) {
  const paused = Boolean(active) || expired;
  const Icon = paused ? Pause : Play;
  const nextText = next
    ? ` Próxima pausa: ${eventName(next)}, ${lowerFirst(relativeDay(Date.parse(next.start), nowMs))}.`
    : '';
  return (
    <section
      aria-labelledby="parado-t"
      data-paused={paused}
      className={cn(
        'flex items-start gap-3.5 rounded-card border border-l-4 bg-surface p-4 shadow-card',
        active ? 'border-warn' : expired ? 'border-loss' : 'border-gain',
      )}
    >
      <Icon
        className={cn(
          'mt-0.5 size-6 shrink-0',
          active ? 'text-warn' : expired ? 'text-loss' : 'text-gain',
        )}
        aria-hidden="true"
      />
      <div className="min-w-0" aria-live="polite">
        <h2 id="parado-t" className="text-[16px] font-bold">
          {paused ? 'O robô está parado agora' : 'Sem evento por perto'}
        </h2>
        <p className="break-words">
          {active
            ? `Por causa de “${eventName(active)}”. Volta a operar às ${brasiliaClock(active.block_end)} (horário de Brasília).`
            : expired
              ? 'Por segurança, até o calendário voltar.'
              : `Ele opera normalmente.${nextText}`}
        </p>
      </div>
    </section>
  );
}
