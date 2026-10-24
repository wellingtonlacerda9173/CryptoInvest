import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { http } from './http';
import {
  ACTIVE_STATUSES,
  backtestJobSchema,
  backtestPageSchema,
  backtestReportSchema,
  type BacktestJob,
} from './backtestSchemas';

export const BACKTEST_KEYS = {
  list: ['backtests'],
  job: (id: string) => ['backtests', 'job', id],
  report: (id: string) => ['backtests', 'report', id],
} as const;

const POLL_MS = 1_500;

export const isActive = (job: BacktestJob | undefined) =>
  job !== undefined && ACTIVE_STATUSES.includes(job.status);

/** Acompanha o job: consulta de 1,5 em 1,5 s enquanto estiver na fila ou rodando. */
export const useBacktestJob = (id: string | null) =>
  useQuery({
    queryKey: BACKTEST_KEYS.job(id ?? ''),
    queryFn: ({ signal }) => http(`/backtests/${id}`, { schema: backtestJobSchema, signal }),
    enabled: id !== null,
    refetchInterval: (q) =>
      isActive(q.state.data) || q.state.data === undefined ? POLL_MS : false,
  });

export const useBacktestReport = (id: string | null, enabled: boolean) =>
  useQuery({
    queryKey: BACKTEST_KEYS.report(id ?? ''),
    queryFn: ({ signal }) =>
      http(`/backtests/${id}/report`, { schema: backtestReportSchema, signal }),
    enabled: id !== null && enabled,
    staleTime: Infinity,
  });

export const useBacktestHistory = () =>
  useInfiniteQuery({
    queryKey: BACKTEST_KEYS.list,
    queryFn: ({ signal, pageParam }) =>
      http(`/backtests?limit=10${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ''}`, {
        schema: backtestPageSchema,
        signal,
      }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.items.some((j) => isActive(j))) ? POLL_MS * 2 : false,
  });

export const backtestApi = {
  create: (body: unknown) =>
    http('/backtests', { method: 'POST', body, schema: backtestJobSchema }),
  cancel: (id: string) =>
    http(`/backtests/${id}/cancel`, { method: 'POST', schema: backtestJobSchema }),
};
