import type { Avoided, WalkForward } from '@/api/backtestSchemas';
import type { ChipTone } from '@/components/kit/Chip';

/** Abaixo disso o resultado nao permite concluir nada (mesma regra da tela Desempenho). */
export const MIN_TRADES = 30;

export interface Verdict {
  label: string;
  text: string;
  tone: ChipTone;
}

/** Quatro veredictos honestos do motor (mais "sem selecao"); nenhum usa verde de ganho nem vermelho de alarme. */
export function verdictOf(raw: string): Verdict {
  if (raw === 'consistente')
    return {
      label: 'Consistente',
      text: 'O resultado fora da amostra acompanha o do treino. Isso não garante o futuro.',
      tone: 'info',
    };
  if (raw.startsWith('inconclusivo'))
    return {
      label: 'Inconclusivo',
      text: 'Poucas operações fora da amostra para concluir qualquer coisa. Amostra pequena.',
      tone: 'warn',
    };
  if (raw.startsWith('sem edge'))
    return {
      label: 'Sem vantagem detectável',
      text: 'Fora da amostra, a estratégia não mostrou vantagem sobre o acaso.',
      tone: 'off',
    };
  if (raw.startsWith('indicios de sobreajuste') || raw.startsWith('indícios de sobreajuste'))
    return {
      label: 'Indícios de sobreajuste',
      text: 'Foi bem no treino e pior no teste: a estratégia pode estar ajustada demais ao passado.',
      tone: 'warn',
    };
  if (raw.startsWith('sem selecao') || raw.startsWith('sem seleção'))
    return {
      label: 'Sem seleção',
      text: 'Nenhum conjunto de parâmetros passou no treino: o robô ficaria parado.',
      tone: 'off',
    };
  return { label: 'Sem veredito', text: raw, tone: 'off' };
}

export const FLAG_TEXT: Record<string, string> = {
  'expectativa em R positiva no treino e nao positiva fora da amostra':
    'A expectativa em R era positiva no treino e deixou de ser fora da amostra.',
  'retorno mensal fora da amostra abaixo de metade do retorno no treino':
    'O retorno mensal fora da amostra ficou abaixo da metade do retorno no treino.',
  'drawdown fora da amostra maior que 1,5x o drawdown medio do treino':
    'A queda máxima fora da amostra foi maior que 1,5 vez a média do treino.',
  'parametros escolhidos mudam quase a cada janela (selecao instavel)':
    'Os parâmetros escolhidos mudam quase a cada janela (seleção instável).',
};

export const flagText = (flag: string) =>
  FLAG_TEXT[flag] ?? flag.replace(/^apenas (\d+) trades/, 'Apenas $1 operações');

export const KIND_LABEL: Record<string, string> = {
  capacity: 'Capacidade',
  state_lock: 'Travas do robô',
  filter: 'Filtros de mercado e risco',
};

export const CODE_LABEL: Record<string, string> = {
  position_already_open: 'Já havia posição aberta',
  entry_pending: 'Entrada já pendente',
  max_positions: 'Limite de posições',
  adding_to_loser: 'Não aumenta posição perdedora',
  robot_halted: 'Robô desligado',
  paused: 'Robô pausado',
  daily_loss_pause: 'Perda diária atingida',
  daily_target_reached: 'Meta diária atingida',
  pair_paused: 'Par em pausa',
};

export const codeLabel = (code: string) => CODE_LABEL[code] ?? code.replace(/_/g, ' ');

export interface AvoidedBlock {
  kind: 'capacity' | 'state_lock' | 'filter';
  title: string;
  explain: string;
  net: number;
}

export function avoidedBlocks(a: Avoided): AvoidedBlock[] {
  return [
    {
      kind: 'capacity',
      title: 'Capacidade',
      explain: 'O robô já tinha posição ou ordem aberta: não é um juízo sobre o sinal.',
      net: a.capacity_net_effect,
    },
    {
      kind: 'state_lock',
      title: 'Travas do robô',
      explain: 'Robô desligado, pausas e metas do dia: protegem o saldo, não avaliam o sinal.',
      net: a.state_locks_net_effect,
    },
    {
      kind: 'filter',
      title: 'Filtros de mercado e risco',
      explain: 'Só estes medem o valor dos vetos: o sinal foi recusado por um critério.',
      net: a.risk_vetoes_net_effect,
    },
  ];
}

export const asError = (wf: WalkForward | null): string | null =>
  wf !== null && 'error' in wf && typeof wf.error === 'string' ? wf.error : null;
