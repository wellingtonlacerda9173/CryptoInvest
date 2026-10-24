import type { RiskLimit } from '@/api/schemas';
import { formatNumber } from '@/lib/format';
import { GLOSSARY, type GlossaryKey } from '@/lib/glossary';
import { GlossaryTerm } from './GlossaryTerm';
import { LimitBar } from './LimitBar';

const TERMS: Record<string, GlossaryKey> = {
  daily_loss: 'perda-diaria',
  drawdown: 'drawdown',
};
const termFor = (key: string): GlossaryKey | undefined => {
  const k = TERMS[key];
  return k && k in GLOSSARY ? k : undefined;
};

function LimitRow({ l }: { l: RiskLimit }) {
  const term = termFor(l.key);
  const digits = l.unit === '%' ? 1 : 0;
  const valueText = `${formatNumber(l.value, digits)}${l.unit} de ${formatNumber(l.limit, 0)}${l.unit}`;
  return (
    <LimitBar
      name={term ? <GlossaryTerm term={term}>{l.name}</GlossaryTerm> : l.name}
      label={l.name}
      valueText={valueText}
      value={l.value}
      limit={l.limit}
      note={l.note}
      level={l.level}
      stateText={
        l.active_until
          ? `${l.status_text} até ${new Date(l.active_until).toISOString().slice(11, 16)} UTC`
          : l.status_text
      }
    />
  );
}

export const RiskLimitsList = ({ limits }: { limits: RiskLimit[] }) => (
  <div className="flex flex-col gap-4">
    {limits.map((l) => (
      <LimitRow key={l.key} l={l} />
    ))}
  </div>
);
