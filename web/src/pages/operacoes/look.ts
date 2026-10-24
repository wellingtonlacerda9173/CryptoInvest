import { ArrowDown, ArrowUp, Minus } from 'lucide-react';

export const RESULT_LOOK = {
  win: { tone: 'ok', icon: ArrowUp, label: 'Ganho' },
  loss: { tone: 'bad', icon: ArrowDown, label: 'Perda' },
  breakeven: { tone: 'off', icon: Minus, label: 'Empate' },
} as const;
