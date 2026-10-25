import { useQuery } from '@tanstack/react-query';
import { calendarSchema } from './calendarSchemas';
import { http } from './http';

export const CALENDAR_KEY = ['calendar'] as const;

/** Muda raramente: consulta a cada 60 s e ao voltar para a aba. */
export const useCalendar = () =>
  useQuery({
    queryKey: CALENDAR_KEY,
    queryFn: ({ signal }) => http('/calendar', { schema: calendarSchema, signal }),
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });

export type CalendarAlert = 'expiring' | 'expired' | null;

export const useCalendarAlert = (): CalendarAlert => {
  const state = useCalendar().data?.state;
  return state === 'expiring' || state === 'expired' ? state : null;
};
