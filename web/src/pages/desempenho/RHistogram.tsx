import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from 'recharts';
import { formatNumber, formatSigned } from '@/lib/format';
import { binR, WIDTH } from './binR';

const label = (from: number) => `${formatSigned(from, 1)} a ${formatSigned(from + WIDTH, 1)}`;

export function RHistogram({ values }: { values: number[] }) {
  const bins = binR(values);
  const data = bins.map((b) => ({ ...b, name: label(b.from) }));
  const losses = values.filter((v) => v < 0).length;
  const summary = `Distribuição de resultados em R de ${values.length} operações: ${losses} com perda, ${values.length - losses} sem perda.`;
  return (
    <div>
      <div role="img" aria-label={summary} className="h-[200px] w-full sm:h-[240px]">
        <ResponsiveContainer
          width="100%"
          height="100%"
          initialDimension={{ width: 500, height: 220 }}
        >
          <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--grid-line)" />
            <XAxis
              dataKey="from"
              tickFormatter={(v: number) => formatSigned(v, 1)}
              tick={{ fill: 'var(--muted)', fontSize: 11 }}
              stroke="var(--border-strong)"
              interval="preserveStartEnd"
              minTickGap={20}
            />
            <YAxis
              allowDecimals={false}
              width={32}
              tick={{ fill: 'var(--muted)', fontSize: 11 }}
              stroke="var(--border-strong)"
            />
            <ReferenceLine x={0} stroke="var(--border-strong)" />
            <Bar dataKey="count" isAnimationActive={false}>
              {data.map((b) => (
                <Cell key={b.from} fill={b.from < 0 ? 'var(--loss)' : 'var(--gain)'} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-2 flex flex-wrap gap-x-4 text-cap text-muted">
        <span>▼ Barras à esquerda do zero: perdas</span>
        <span>▲ À direita: ganhos</span>
      </p>
      <table className="sr-only">
        <caption>Operações por faixa de resultado em R</caption>
        <thead>
          <tr>
            <th scope="col">Faixa (R)</th>
            <th scope="col">Operações</th>
          </tr>
        </thead>
        <tbody>
          {data.map((b) => (
            <tr key={b.from}>
              <th scope="row">{b.name}</th>
              <td>{formatNumber(b.count, 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
