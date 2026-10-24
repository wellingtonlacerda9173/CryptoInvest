export type TradingMode = 'backtest' | 'paper' | 'live';

export const MODE_META: Record<TradingMode, { label: string; hint: string; money: string }> = {
  backtest: { label: 'BACKTEST', hint: 'histórico', money: 'Saldo' },
  paper: { label: 'PAPER', hint: 'simulado', money: 'Saldo (simulado)' },
  live: { label: 'LIVE', hint: 'dinheiro real', money: 'Saldo (real)' },
};
