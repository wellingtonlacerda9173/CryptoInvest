import { useEffect, useRef, useState } from 'react';

export interface SocketLike {
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  close(): void;
  send?(data: string): void;
}
export type SocketFactory = (url: string) => SocketLike;

export const browserSocketFactory: SocketFactory = (url) =>
  new WebSocket(url) as unknown as SocketLike;

export type SocketState = 'connecting' | 'open' | 'reconnecting';

/** Espera exponencial (1 s, 2 s, 4 s ... ate 30 s) com jitter de 50-100% para nao sincronizar clientes. */
export const nextDelay = (attempt: number, random: () => number = Math.random) =>
  Math.min(30_000, 1_000 * 2 ** attempt) * (0.5 + random() * 0.5);

interface Options {
  url: string;
  factory?: SocketFactory;
  onMessage: (data: unknown, send: (data: unknown) => void) => void;
  onOpen?: (reconnected: boolean) => void;
  backoff?: (attempt: number) => number;
  enabled?: boolean;
  /** Silencio maximo (ms) sem nenhuma mensagem/ping antes de tratar a conexao como morta. */
  staleAfterMs?: () => number;
}

export const DEFAULT_STALE_AFTER_MS = 50_000;
const WATCHDOG_TICK_MS = 5_000;

export function useReconnectingSocket({
  url,
  factory = browserSocketFactory,
  onMessage,
  onOpen,
  backoff = nextDelay,
  enabled = true,
  staleAfterMs,
}: Options) {
  const [state, setState] = useState<SocketState>('connecting');
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null);
  const onMessageRef = useRef(onMessage);
  const onOpenRef = useRef(onOpen);
  const backoffRef = useRef(backoff);
  const factoryRef = useRef(factory);
  const staleRef = useRef(staleAfterMs);

  useEffect(() => {
    onMessageRef.current = onMessage;
    onOpenRef.current = onOpen;
    backoffRef.current = backoff;
    factoryRef.current = factory;
    staleRef.current = staleAfterMs;
  });

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let attempt = 0;
    let everOpened = false;
    let socket: SocketLike | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let lastActivity = Date.now();

    const connect = () => {
      if (disposed) return;
      try {
        socket = factoryRef.current(url);
      } catch {
        scheduleRetry();
        return;
      }
      const current = socket;
      const send = (data: unknown) => current.send?.(JSON.stringify(data));
      lastActivity = Date.now();
      socket.onopen = () => {
        lastActivity = Date.now();
        attempt = 0;
        setState('open');
        onOpenRef.current?.(everOpened);
        everOpened = true;
      };
      socket.onmessage = (ev) => {
        lastActivity = Date.now();
        setLastMessageAt(lastActivity);
        let payload: unknown = ev.data;
        if (typeof payload === 'string') {
          try {
            payload = JSON.parse(payload);
          } catch {
            return;
          }
        }
        onMessageRef.current(payload, send);
      };
      socket.onclose = () => {
        socket = null;
        scheduleRetry();
      };
      socket.onerror = () => {
        // o `close` que sempre segue o erro agenda a nova tentativa
      };
    };

    const scheduleRetry = () => {
      if (disposed) return;
      setState('reconnecting');
      timer = setTimeout(connect, backoffRef.current(attempt++));
    };

    // Conexao "aberta" que nao entrega nada (nem o ping do servidor) esta morta: derruba e reconecta.
    const watchdog = setInterval(() => {
      if (disposed || !socket) return;
      if (Date.now() - lastActivity <= (staleRef.current?.() ?? DEFAULT_STALE_AFTER_MS)) return;
      const dead = socket;
      socket = null;
      dead.onclose = null;
      dead.onmessage = null;
      dead.close();
      scheduleRetry();
    }, WATCHDOG_TICK_MS);

    connect();
    return () => {
      disposed = true;
      clearInterval(watchdog);
      clearTimeout(timer);
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, [url, enabled]);

  return { state, lastMessageAt };
}
