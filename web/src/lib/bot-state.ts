export const BOT_STATES = [
  'STARTING',
  'RUNNING',
  'PAUSED',
  'FAILED_PERSISTENCE',
  'HALTED',
  'STOPPING',
  'STOPPED',
  'OFFLINE',
] as const;
export type BotState = (typeof BOT_STATES)[number];

export type StatusTone = 'ok' | 'warn' | 'bad' | 'off' | 'offline';

export interface StatusFace {
  tone: StatusTone;
  label: string;
  reason: string;
}

interface StatusInput {
  state: BotState;
  stopsBlind: boolean;
  reason: string | null;
}

export function describeBotState({ state, stopsBlind, reason }: StatusInput): StatusFace {
  if (stopsBlind && state !== 'HALTED' && state !== 'FAILED_PERSISTENCE') {
    return { tone: 'warn', label: 'Stops cegos', reason: reason ?? 'sem dados de mercado' };
  }
  switch (state) {
    case 'RUNNING':
      return { tone: 'ok', label: 'Operando', reason: reason ?? 'aguardando sinal' };
    case 'PAUSED':
      return { tone: 'warn', label: 'Pausado', reason: reason ?? 'novas entradas bloqueadas' };
    case 'HALTED':
      return {
        tone: 'bad',
        label: 'DESLIGADO por drawdown',
        reason: reason ?? 'só você pode religar',
      };
    case 'FAILED_PERSISTENCE':
      return {
        tone: 'bad',
        label: 'Falha ao gravar',
        reason: reason ?? 'entradas bloqueadas; stops ativos',
      };
    case 'STARTING':
      return { tone: 'off', label: 'Iniciando…', reason: reason ?? 'conferindo posições' };
    case 'STOPPING':
      return { tone: 'off', label: 'Encerrando…', reason: reason ?? 'parada ordenada' };
    case 'STOPPED':
      return { tone: 'off', label: 'Parado', reason: reason ?? 'sem novas entradas' };
    case 'OFFLINE':
      return { tone: 'off', label: 'Robô desligado', reason: reason ?? 'sem sinal do robô' };
  }
}

export const DISCONNECTED_FACE: StatusFace = {
  tone: 'offline',
  label: 'SEM CONEXÃO COM O PAINEL',
  reason: 'dados desatualizados',
};

export const NO_SIGNAL_FACE: StatusFace = {
  tone: 'offline',
  label: 'ROBÔ SEM SINAL',
  reason: 'último estado conhecido',
};

/** Prioridade: painel sem conexao > robo sem sinal (WS) > estado real do robo. */
export function resolveTopBarFace(input: {
  disconnected: boolean;
  runnerConnected: boolean | null;
  status: StatusInput | null;
  loadFailed: boolean;
  loading: StatusFace;
  failed: StatusFace;
}): StatusFace {
  if (input.disconnected) return DISCONNECTED_FACE;
  if (input.runnerConnected === false) return NO_SIGNAL_FACE;
  if (input.status) return describeBotState(input.status);
  return input.loadFailed ? input.failed : input.loading;
}
