import {
  useInfiniteQuery,
  useQuery,
  type InfiniteData,
  type QueryClient,
} from '@tanstack/react-query';
import { API_BASE, ApiError, http } from './http';
import { candlesSchema, performanceSchema, tradePageSchema, type Timeframe } from './marketSchemas';
import { KEYS, useBotStatus } from './queries';
import { decisionPageSchema, type DecisionPage } from './schemas';

export type ViewMode = 'paper' | 'live';

/** Modo exibido nas listas: o do robo (live so quando ele esta em live); totais de modos nunca se misturam. */
export const useViewMode = (): ViewMode =>
  useBotStatus().data?.mode === 'live' ? 'live' : 'paper';

const queryString = (params: Record<string, string | number | boolean | null | undefined>) => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== '') q.set(k, String(v));
  }
  const text = q.toString();
  return text ? `?${text}` : '';
};

export const useCandles = (symbol: string | undefined, timeframe: Timeframe, limit = 300) =>
  useQuery({
    queryKey: [...KEYS.candles, symbol, timeframe, limit],
    queryFn: ({ signal }) =>
      http(`/market/candles${queryString({ symbol, timeframe, limit })}`, {
        schema: candlesSchema,
        signal,
      }),
    enabled: Boolean(symbol),
    refetchInterval: 20_000,
  });

export const usePerformance = (days: number | null) =>
  useQuery({
    queryKey: [...KEYS.performance, days],
    queryFn: ({ signal }) =>
      http(`/performance${queryString({ days })}`, { schema: performanceSchema, signal }),
  });

export interface TradeFilters {
  symbol?: string;
  result?: 'win' | 'loss' | 'breakeven';
  exit_reason?: string;
  since?: string;
  q?: string;
}

export const useTrades = (mode: ViewMode, filters: TradeFilters, limit = 25) =>
  useInfiniteQuery({
    queryKey: [...KEYS.trades, mode, filters, limit],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam, signal }) =>
      http(`/trades${queryString({ mode, limit, cursor: pageParam, ...filters })}`, {
        schema: tradePageSchema,
        signal,
      }),
    getNextPageParam: (last) => last.next_cursor ?? null,
  });

export interface DecisionFilters {
  traded?: boolean;
  symbol?: string;
}

/** Teto de paginas carregadas na linha do tempo: acima disso o painel pede para filtrar (memoria e rede limitadas). */
export const DECISION_FEED_MAX_PAGES = 20;
const FEED_KEY = [...KEYS.decisions, 'feed'] as const;

const fetchDecisionPage = (
  mode: ViewMode,
  filters: DecisionFilters,
  limit: number,
  cursor: number | null,
  signal?: AbortSignal,
) =>
  http(`/decisions${queryString({ mode, limit, cursor, ...filters })}`, {
    schema: decisionPageSchema,
    signal,
  });

export const useDecisionFeed = (mode: ViewMode, filters: DecisionFilters, limit = 25) =>
  useInfiniteQuery({
    queryKey: [...FEED_KEY, mode, filters, limit],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam, signal }) => fetchDecisionPage(mode, filters, limit, pageParam, signal),
    getNextPageParam: (last) => last.next_cursor ?? null,
  });

/**
 * Evento novo no WS: busca so a primeira pagina e junta as decisoes inéditas ao topo do feed ja carregado,
 * em vez de refazer todas as paginas. As paginas antigas e o cursor delas ficam intactos.
 */
export async function refreshDecisionFeeds(qc: QueryClient): Promise<void> {
  const feeds = qc.getQueryCache().findAll({ queryKey: FEED_KEY, type: 'active' });
  await Promise.all(
    feeds.map(async ({ queryKey }) => {
      const [, , mode, filters, limit] = queryKey as [
        string,
        string,
        ViewMode,
        DecisionFilters,
        number,
      ];
      try {
        const fresh = await fetchDecisionPage(mode, filters, limit, null);
        qc.setQueryData<InfiniteData<DecisionPage, number | null>>(queryKey, (old) => {
          const head = old?.pages[0];
          if (!old || !head) return old;
          const known = new Set(old.pages.flatMap((p) => p.items.map((d) => d.id)));
          const added = fresh.items.filter((d) => !known.has(d.id));
          const first = { ...head, summary: fresh.summary, items: [...added, ...head.items] };
          return { ...old, pages: [first, ...old.pages.slice(1)] };
        });
      } catch {
        void qc.invalidateQueries({ queryKey, refetchType: 'active' });
      }
    }),
  );
}

/** O CSV e montado pelo servidor (que neutraliza formulas); o painel so entrega o arquivo recebido. */
export async function downloadTradesCsv(mode: ViewMode, filters: TradeFilters): Promise<string> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/trades/export${queryString({ mode, ...filters })}`, {
      credentials: 'include',
      headers: { Accept: 'text/csv' },
    });
  } catch {
    throw new ApiError(0, { title: 'Sem conexão' });
  }
  if (!res.ok) throw new ApiError(res.status, { title: 'Não foi possível exportar' });
  const blob = await res.blob();
  const header = res.headers.get('Content-Disposition') ?? '';
  const name = /filename="([A-Za-z0-9._-]+)"/.exec(header)?.[1] ?? `operacoes-${mode}.csv`;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return name;
}
