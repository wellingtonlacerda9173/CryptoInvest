import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from 'recharts';
import type { EquitySeries } from '@/api/schemas';
import { formatNumber } from '@/lib/format';

const dayLabel = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit' }).format(new Date(iso));

/** Curva de capital: cores so dos tokens; resumo textual e tabela alternativa para leitores de tela. */
export function EquityChart({ series, compact }: { series: EquitySeries; compact?: boolean }) {
  const points = series.points;
  const first = points[0];
  const last = points[points.length - 1];
  if (!first || !last) return null;
  const initial = series.initial_capital;
  const values = points.map((p) => p.equity);
  const lo = Math.min(...values, initial ?? Infinity);
  const hi = Math.max(...values, initial ?? -Infinity);
  const pad = Math.max((hi - lo) * 0.15, hi * 0.001);
  const domain: [number, number] = [Math.floor(lo - pad), Math.ceil(hi + pad)];
  const summary = `Curva de capital${series.simulated ? ' simulada' : ''}, de ${formatNumber(first.equity)} para ${formatNumber(last.equity)} USDT, ${points.length} pontos entre ${dayLabel(first.at)} e ${dayLabel(last.at)}.`;

  return (
    <div>
      <div role="img" aria-label={summary} className="h-[180px] w-full sm:h-[220px]">
        <ResponsiveContainer
          width="100%"
          height="100%"
          initialDimension={{ width: 600, height: 200 }}
        >
          <AreaChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--grid-line)" />
            <XAxis
              dataKey="at"
              tickFormatter={dayLabel}
              tick={{ fill: 'var(--muted)', fontSize: 11 }}
              stroke="var(--border-strong)"
              minTickGap={compact ? 48 : 32}
              tickMargin={6}
            />
            <YAxis
              domain={domain}
              width={compact ? 48 : 56}
              tickFormatter={(v: number) => formatNumber(v, 0)}
              tick={{ fill: 'var(--muted)', fontSize: 11 }}
              stroke="var(--border-strong)"
              tickCount={compact ? 4 : 6}
            />
            {initial !== null ? (
              <ReferenceLine y={initial} stroke="var(--border-strong)" strokeDasharray="4 4" />
            ) : null}
            <Area
              type="monotone"
              dataKey="equity"
              stroke="var(--info)"
              strokeWidth={2.5}
              fill="var(--info)"
              fillOpacity={0.12}
              isAnimationActive={false}
              dot={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-cap text-muted">
        <span className="inline-flex items-center gap-1.5">
          <i aria-hidden="true" className="inline-block h-[3px] w-3.5 rounded-sm bg-info" />
          {series.simulated ? 'Saldo simulado' : 'Saldo'}
        </span>
        {initial !== null ? (
          <span>Linha tracejada: capital inicial ({formatNumber(initial, 0)})</span>
        ) : null}
      </div>
      <table className="sr-only">
        <caption>Dados da curva de capital</caption>
        <thead>
          <tr>
            <th scope="col">Data</th>
            <th scope="col">Saldo (USDT)</th>
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.at}>
              <th scope="row">{dayLabel(p.at)}</th>
              <td>{formatNumber(p.equity)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
