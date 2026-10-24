import type { GroupStats } from '@/api/marketSchemas';
import { directionOf, formatNumber, formatPercent, formatSigned } from '@/lib/format';
import { cn } from '@/lib/utils';

export function GroupTable({
  caption,
  firstColumn,
  rows,
  formatKey = (k) => k,
}: {
  caption: string;
  firstColumn: string;
  rows: GroupStats[];
  formatKey?: (key: string) => string;
}) {
  return (
    <div tabIndex={0} role="region" aria-label={`${caption}: tabela`} className="overflow-x-auto">
      <table className="w-full min-w-[420px] border-collapse text-left text-sec tabular-nums">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="text-muted">
            {[firstColumn, 'Operações', 'Acerto', 'Profit factor', 'Resultado (USDT)'].map((h) => (
              <th key={h} scope="col" className="border-b border-border px-2 py-1.5 font-semibold">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const dir = directionOf(r.pnl);
            return (
              <tr key={r.key}>
                <th scope="row" className="px-2 py-2 font-semibold">
                  {formatKey(r.key)}
                </th>
                <td className="px-2 py-2">{r.trades}</td>
                <td className="px-2 py-2">
                  {r.win_rate === null ? '—' : formatPercent(r.win_rate * 100, 0)}
                </td>
                <td className="px-2 py-2">
                  {r.profit_factor === null ? '—' : formatNumber(r.profit_factor)}
                </td>
                <td
                  className={cn(
                    'px-2 py-2 font-semibold',
                    dir === 'up' ? 'text-gain' : dir === 'down' ? 'text-loss' : 'text-muted',
                  )}
                >
                  <span aria-hidden="true">{dir === 'up' ? '▲ ' : dir === 'down' ? '▼ ' : ''}</span>
                  {formatSigned(r.pnl)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
