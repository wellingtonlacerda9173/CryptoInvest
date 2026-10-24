import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import {
  createContext,
  use,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { API_BASE } from '@/api/http';
import { refreshDecisionFeeds } from '@/api/marketQueries';
import { KEYS } from '@/api/queries';
import { wsMessageSchema, type WsChannel } from './messages';
import {
  DEFAULT_STALE_AFTER_MS,
  useReconnectingSocket,
  type SocketFactory,
  type SocketState,
} from './useReconnectingSocket';

interface ConnectionApi {
  state: SocketState;
  /** Falso so depois de uma queda: a conexao inicial ainda nao e "desconectado". */
  disconnected: boolean;
  lastMessageAt: number | null;
  /** Falso quando a API avisa que o robo parou de dar sinal (a API segue de pe). */
  runnerConnected: boolean | null;
}

const ConnectionContext = createContext<ConnectionApi>({
  state: 'connecting',
  disconnected: false,
  lastMessageAt: null,
  runnerConnected: null,
});

const CHANNELS: WsChannel[] = [
  'status',
  'decisions',
  'orders',
  'trades',
  'equity',
  'alerts',
  'heartbeat',
];

/** `API_BASE` relativo ('/api') usa o host da pagina; absoluto ('https://api.x/api') usa o host dele. */
export function defaultSocketUrl(
  base: string = API_BASE,
  loc: Pick<Location, 'protocol' | 'host'> = location,
) {
  const query = `/ws?channels=${CHANNELS.join(',')}`;
  if (/^https?:\/\//i.test(base))
    return `${base.replace(/^http/i, 'ws').replace(/\/+$/, '')}${query}`;
  return `${loc.protocol === 'https:' ? 'wss' : 'ws'}://${loc.host}${base}${query}`;
}

/** Decisao nova: revalida o resumo e junta a primeira pagina do feed, sem refazer as paginas antigas. */
const DECISIONS_HEAD = ['decisions-head'] as const;

const KEYS_BY_CHANNEL: Record<WsChannel, readonly (readonly string[])[]> = {
  status: [KEYS.status, KEYS.limits, KEYS.commands],
  heartbeat: [KEYS.status],
  alerts: [KEYS.status, KEYS.limits],
  decisions: [DECISIONS_HEAD, KEYS.candles],
  orders: [KEYS.positions, KEYS.status, KEYS.limits, KEYS.candles],
  trades: [
    KEYS.positions,
    KEYS.account,
    KEYS.status,
    KEYS.limits,
    KEYS.trades,
    KEYS.performance,
    KEYS.candles,
  ],
  equity: [KEYS.equity, KEYS.account, KEYS.status],
};
const ALL_KEYS = [
  ...new Set(
    Object.values(KEYS_BY_CHANNEL)
      .flat()
      .filter((k) => k !== DECISIONS_HEAD),
  ),
  KEYS.decisions,
];

/** Junta rajadas de eventos numa unica revalidacao (o painel mostra o numero, nao faz conta). */
function useCoalescedInvalidate(queryClient: QueryClient) {
  const pending = useRef(new Set<readonly string[]>());
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return useCallback(
    (keys: readonly (readonly string[])[]) => {
      keys.forEach((k) => pending.current.add(k));
      if (timer.current) return;
      timer.current = setTimeout(() => {
        timer.current = undefined;
        const batch = [...pending.current];
        pending.current.clear();
        batch.forEach((queryKey) => {
          if (queryKey === DECISIONS_HEAD) {
            void queryClient.invalidateQueries({
              queryKey: KEYS.decisions,
              predicate: (q) => q.queryKey[1] !== 'feed',
            });
            void refreshDecisionFeeds(queryClient);
          } else void queryClient.invalidateQueries({ queryKey });
        });
      }, 400);
    },
    [queryClient],
  );
}

export function ConnectionProvider({
  url = defaultSocketUrl(),
  factory,
  enabled = true,
  children,
}: {
  url?: string;
  factory?: SocketFactory;
  enabled?: boolean;
  children: ReactNode;
}) {
  const queryClient = useQueryClient();
  const invalidate = useCoalescedInvalidate(queryClient);
  const [runnerConnected, setRunnerConnected] = useState<boolean | null>(null);
  const pingSeconds = useRef<number | null>(null);
  const staleAfterMs = useCallback(
    () => (pingSeconds.current ? pingSeconds.current * 2.5 * 1000 : DEFAULT_STALE_AFTER_MS),
    [],
  );

  const onMessage = useCallback(
    (data: unknown, send: (data: unknown) => void) => {
      const parsed = wsMessageSchema.safeParse(data);
      if (!parsed.success) return;
      const msg = parsed.data;
      switch (msg.type) {
        case 'ping':
          send({ type: 'pong' });
          break;
        case 'hello':
          setRunnerConnected(msg.runner_connected);
          pingSeconds.current = msg.ping_seconds ?? null;
          break;
        case 'runner':
          setRunnerConnected(msg.connected);
          invalidate([KEYS.status]);
          break;
        case 'resync':
          invalidate(ALL_KEYS);
          break;
        case 'event':
          invalidate(KEYS_BY_CHANNEL[msg.channel]);
          break;
        default:
          break;
      }
    },
    [invalidate],
  );

  // Ao reabrir depois de uma queda, o que passou nesse intervalo nao chega por evento: refaz a leitura.
  const onOpen = useCallback(
    (reconnected: boolean) => {
      if (reconnected) invalidate(ALL_KEYS);
    },
    [invalidate],
  );

  const { state, lastMessageAt } = useReconnectingSocket({
    url,
    factory,
    onMessage,
    onOpen,
    enabled,
    staleAfterMs,
  });
  const value = useMemo(
    () => ({ state, disconnected: state === 'reconnecting', lastMessageAt, runnerConnected }),
    [state, lastMessageAt, runnerConnected],
  );
  return <ConnectionContext value={value}>{children}</ConnectionContext>;
}

export const useConnection = () => use(ConnectionContext);
