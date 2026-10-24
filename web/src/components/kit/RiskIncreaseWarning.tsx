import { TriangleAlert } from 'lucide-react';

export interface RiskIncreaseWarningProps {
  before: string;
  after: string;
  costAfter?: string;
  costBefore?: string;
  /** Frase pronta do servidor (substitui o par costAfter/costBefore). */
  costText?: string;
  label?: string;
  currency?: string;
}

export function RiskIncreaseWarning({
  before,
  after,
  costAfter,
  costBefore,
  costText,
  label,
  currency = 'USDT',
}: RiskIncreaseWarningProps) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2.5 rounded-xl border border-warn bg-warn-bg px-3 py-2.5 text-sec"
    >
      <TriangleAlert className="mt-px size-5 shrink-0 text-warn" aria-hidden="true" />
      <div>
        <strong>Isto aumenta o risco.</strong> {label ? `${label}. ` : ''}Antes: {before}. Agora:{' '}
        {after}.{' '}
        {costText ? (
          costText
        ) : (
          <>
            Cada perda custará até{' '}
            <span className="tabular-nums">
              {costAfter} {currency}
            </span>{' '}
            (hoje: <span className="tabular-nums">{costBefore}</span>).
          </>
        )}{' '}
        Confirme só se for isso mesmo que você quer.
      </div>
    </div>
  );
}
