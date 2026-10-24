import { ArrowDown, ArrowUp, CircleCheck, TriangleAlert } from 'lucide-react';
import type { Position } from '@/api/schemas';
import { directionOf, formatNumber, formatSigned } from '@/lib/format';
import { cn } from '@/lib/utils';
import { Chip } from './Chip';

export function PositionsTable({ positions }: { positions: Position[] }) {
  return (
    <div
      tabIndex={0}
      role="region"
      aria-label="Posições abertas"
      className="max-w-full min-w-0 overflow-x-auto overscroll-x-contain"
    >
      <table className="w-full min-w-[640px] border-collapse text-left">
        <thead>
          <tr className="text-sec text-muted">
            {['Par', 'Entrada', 'Stop', 'Alvo', 'Tamanho', 'Resultado', 'Proteção'].map((h) => (
              <th key={h} scope="col" className="border-b border-border px-2 py-2 font-semibold">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const dir = p.unrealized_pnl === null ? 'flat' : directionOf(p.unrealized_pnl);
            const Arrow = dir === 'up' ? ArrowUp : ArrowDown;
            return (
              <tr key={`${p.run_id}-${p.symbol}`} className="tabular-nums">
                <th scope="row" className="px-2 py-2.5 font-semibold">
                  {p.symbol}
                </th>
                <td className="px-2 py-2.5">{formatNumber(p.entry_price)}</td>
                <td className="px-2 py-2.5">{formatNumber(p.stop)}</td>
                <td className="px-2 py-2.5">
                  {p.take_profit === null ? '—' : formatNumber(p.take_profit)}
                </td>
                <td className="px-2 py-2.5">{formatNumber(p.quantity, 4)}</td>
                <td
                  className={cn(
                    'px-2 py-2.5 font-semibold',
                    dir === 'up' ? 'text-gain' : dir === 'down' ? 'text-loss' : 'text-muted',
                  )}
                >
                  {p.unrealized_pnl === null ? (
                    '—'
                  ) : (
                    <span className="inline-flex items-center gap-1">
                      {dir !== 'flat' ? <Arrow className="size-4" aria-hidden="true" /> : null}
                      {formatSigned(p.unrealized_pnl)}
                      {p.unrealized_r !== null ? ` (${formatSigned(p.unrealized_r, 1)} R)` : ''}
                    </span>
                  )}
                </td>
                <td className="px-2 py-2.5">
                  {p.protected ? (
                    <Chip tone="ok" icon={CircleCheck}>
                      Stop ativo
                    </Chip>
                  ) : (
                    <Chip tone="bad" icon={TriangleAlert}>
                      {p.protection_note || 'Sem proteção'}
                    </Chip>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
