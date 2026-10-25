import { useEffect, useMemo, useState } from 'react';
import { useCalendar } from '@/api/calendarQueries';
import { activeEvent, calendarView, nextEvent } from '@/lib/calendar';

const DAY_MS = 86_400_000;

/** Relogio do servidor como base: relogio local a cada 1 s (nunca antes da ultima resposta) + deslocamento dela. */
export function useCalendarNow(serverNow: string | undefined, receivedAt: number) {
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setClock(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);
  const offset = serverNow && receivedAt ? Date.parse(serverNow) - receivedAt : 0;
  return Math.max(clock, receivedAt) + offset;
}

export function useCalendario() {
  const query = useCalendar();
  const data = query.data;
  const nowMs = useCalendarNow(data?.now, query.dataUpdatedAt);
  const events = useMemo(
    () => (data?.upcoming ?? []).filter((e) => Date.parse(e.block_end) >= nowMs),
    [data?.upcoming, nowMs],
  );
  const active = activeEvent(events, nowMs);
  const staleSince = query.isError && data ? query.dataUpdatedAt : null;
  const view = data
    ? calendarView(data, nowMs, { hasActive: Boolean(active), stale: staleSince !== null })
    : null;
  const overdueDays =
    view === 'expired' && data?.valid_until && nowMs > Date.parse(data.valid_until)
      ? Math.floor((nowMs - Date.parse(data.valid_until)) / DAY_MS)
      : null;

  return {
    query,
    data,
    nowMs,
    events,
    view,
    active,
    next: nextEvent(events, nowMs),
    overdueDays,
    staleSince,
  };
}
