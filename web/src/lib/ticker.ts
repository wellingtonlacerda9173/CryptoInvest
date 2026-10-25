export const STALE_AFTER_S = 15;
export const DROP_AFTER_S = 120;

export type LiveState = 'live' | 'delayed' | 'none';

/** Idade real da cotacao: a que o servidor informou mais o tempo desde que o painel a recebeu. */
export function resolveLiveState(
  quote: { age_seconds: number; stale: boolean } | undefined,
  receivedAt: number,
  now: number,
): LiveState {
  if (!quote || !receivedAt) return 'none';
  const age = quote.age_seconds + Math.max(0, now - receivedAt) / 1000;
  if (age > DROP_AFTER_S) return 'none';
  return quote.stale || age > STALE_AFTER_S ? 'delayed' : 'live';
}

const PAIR = /^[A-Z0-9]{2,12}\/[A-Z0-9]{2,12}$/;
export const MAX_TICKER_SYMBOLS = 8;

/** Pares enviados a API: o escolhido primeiro, so os que o servidor aceita, sem repetir, ate o limite. */
export function tickerSymbols(symbols: readonly string[], first?: string): string[] {
  const ordered = first ? [first, ...symbols] : [...symbols];
  return [...new Set(ordered.filter((s) => PAIR.test(s)))].slice(0, MAX_TICKER_SYMBOLS);
}

export const priceDigits = (price: number) => (price >= 100 ? 2 : 4);
