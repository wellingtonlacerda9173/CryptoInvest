import type { ChartDecision } from '@/api/marketSchemas';

export type Tier = ChartDecision['tier'];
export const TIER_RANK: Record<Tier, number> = { entered: 3, blocked: 2, quiet: 1 };
export const MAX_DECISIONS = 200;

export interface DecisionGroup {
  /** Inicio da vela onde as decisoes caem (s). */
  time: number;
  tier: Tier;
  items: ChartDecision[];
}

const seconds = (iso: string) => Math.floor(new Date(iso).getTime() / 1000);

/** Ultima vela que abre ate `at`; fora do alcance das velas mostradas, nada a desenhar. */
function snap(times: readonly number[], at: number, tfSec: number): number | null {
  let lo = 0;
  let hi = times.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if ((times[mid] ?? 0) <= at) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  const time = found < 0 ? null : (times[found] ?? null);
  return time !== null && at - time < tfSec ? time : null;
}

/** Uma marca por vela: vale a mais forte (entrou > viu e nao entrou > sem sinal). */
export function groupDecisions(
  decisions: readonly ChartDecision[],
  barTimes: readonly number[],
  tfSec: number,
  showQuiet: boolean,
): DecisionGroup[] {
  const groups = new Map<number, DecisionGroup>();
  for (const d of decisions.slice(-MAX_DECISIONS)) {
    if (d.tier === 'quiet' && !showQuiet) continue;
    const time = snap(barTimes, seconds(d.t), tfSec);
    if (time === null) continue;
    const g = groups.get(time);
    if (!g) groups.set(time, { time, tier: d.tier, items: [d] });
    else {
      g.items.push(d);
      if (TIER_RANK[d.tier] > TIER_RANK[g.tier]) g.tier = d.tier;
    }
  }
  return [...groups.values()].sort((a, b) => a.time - b.time);
}

export const TIER_TEXT: Record<Tier, string> = {
  entered: 'Entrou',
  blocked: 'Viu oportunidade, não entrou',
  quiet: 'Sem sinal',
};
export const TIER_GLYPH: Record<Tier, string> = { entered: '●', blocked: '◆', quiet: '·' };

export const decisionSentence = (d: ChartDecision) =>
  d.tier === 'entered' ? `Entrou: ${d.text}` : `${d.label}: ${d.text}`;
