import { useQueryClient } from '@tanstack/react-query';
import { ChartLine, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { usePerformance, useTrades, useViewMode } from '@/api/marketQueries';
import type { GroupStats } from '@/api/marketSchemas';
import { KEYS, useEquity } from '@/api/queries';
import { CardError } from '@/components/kit/CardError';
import { EmptyState } from '@/components/kit/EmptyState';
import { EquityChart } from '@/components/kit/EquityChart';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { KpiCard, type KpiTone } from '@/components/kit/KpiCard';
import { SimulatedTag } from '@/components/kit/SimulatedTag';
import { PageHeader } from '@/components/layout/PageHeader';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { LabeledSelect } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { formatNumber, formatPercent, formatSigned } from '@/lib/format';
import { GroupTable } from './desempenho/GroupTable';
import { RHistogram } from './desempenho/RHistogram';

const PERIODS = [
  ['30', 'Últimos 30 dias'],
  ['90', 'Últimos 90 dias'],
  ['365', 'Último ano'],
  ['all', 'Tudo'],
] as const;

const monthLabel = (key: string) => {
  const m = /^(\d{4})-(\d{2})/.exec(key);
  return m ? `${m[2]}/${m[1]}` : key;
};

const tone = (v: number | null, good: number): KpiTone =>
  v === null ? 'neutral' : v >= good ? 'gain' : 'warn';

const skeletons = (n: number, h: string) => (
  <div className="flex flex-col gap-3" aria-busy="true">
    {Array.from({ length: n }, (_, i) => (
      <Skeleton key={i} className={h} />
    ))}
  </div>
);

export default function DesempenhoPage() {
  const qc = useQueryClient();
  const mode = useViewMode();
  const [period, setPeriod] = useState<string>('30');
  const days = period === 'all' ? null : Number(period);
  const perf = usePerformance(days);
  const equity = useEquity(days ?? 365);
  const trades = useTrades(mode, {}, 200);
  const p = perf.data;
  const retry = (key: readonly string[]) => () => void qc.invalidateQueries({ queryKey: key });
  const none = p !== undefined && p.trades === 0;
  const rValues = (trades.data?.pages[0]?.items ?? []).flatMap((t) =>
    t.r_multiple === null ? [] : [t.r_multiple],
  );
  const histogramTruncated = (trades.data?.pages[0]?.next_cursor ?? null) !== null;

  const groups = (
    title: string,
    rows: GroupStats[] | undefined,
    first: string,
    fmt?: (k: string) => string,
  ) => (
    <Card aria-labelledby={`g-${first}`}>
      <CardHeader>
        <CardTitle id={`g-${first}`}>{title}</CardTitle>
      </CardHeader>
      {rows ? (
        rows.length > 0 ? (
          <GroupTable caption={title} firstColumn={first} rows={rows} formatKey={fmt} />
        ) : (
          <p className="text-sec text-muted">Sem operações encerradas neste período.</p>
        )
      ) : perf.isError ? (
        <CardError error={perf.error} onRetry={retry(KEYS.performance)} />
      ) : (
        skeletons(3, 'h-8')
      )}
    </Card>
  );

  return (
    <>
      <PageHeader
        title="Desempenho"
        description="O robô está funcionando de verdade? Primeiro o risco, depois o lucro."
      />

      <div className="flex flex-wrap items-end justify-between gap-3">
        <LabeledSelect label="Período" value={period} onChange={(e) => setPeriod(e.target.value)}>
          {PERIODS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </LabeledSelect>
        {p?.simulated ? <SimulatedTag>RESULTADO SIMULADO</SimulatedTag> : null}
      </div>

      {p?.sample_warning ? (
        <div
          role="note"
          className="flex items-start gap-2 rounded-xl border border-warn bg-warn-bg px-3.5 py-2.5 text-sec"
        >
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
          <span>{p.sample_warning}</span>
        </div>
      ) : null}

      {perf.isError ? (
        <CardError
          error={perf.error}
          onRetry={retry(KEYS.performance)}
          retrying={perf.isFetching}
          updatedAt={perf.dataUpdatedAt || undefined}
        />
      ) : null}

      <section
        aria-label="Indicadores de desempenho"
        className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4"
      >
        <KpiCard
          loading={perf.isPending}
          label={<GlossaryTerm term="drawdown">Drawdown máximo</GlossaryTerm>}
          value={p?.max_drawdown_pct != null ? formatPercent(p.max_drawdown_pct, 1) : '—'}
          delta="a maior queda desde um pico"
        />
        <KpiCard
          loading={perf.isPending}
          label={<GlossaryTerm term="profit-factor" />}
          value={p?.profit_factor != null ? formatNumber(p.profit_factor) : '—'}
          tone={tone(p?.profit_factor ?? null, 1)}
          delta={
            p?.profit_factor != null
              ? p.profit_factor >= 1
                ? 'ganha mais do que perde'
                : 'perde mais do que ganha'
              : undefined
          }
          glyph={false}
        />
        <KpiCard
          loading={perf.isPending}
          label={<GlossaryTerm term="taxa-de-acerto" />}
          value={p?.win_rate != null ? formatPercent(p.win_rate * 100, 0) : '—'}
          delta={p ? `${p.wins} ganhos · ${p.losses} perdas` : undefined}
        />
        <KpiCard
          loading={perf.isPending}
          label={<GlossaryTerm term="payoff" />}
          value={p?.payoff != null ? formatNumber(p.payoff) : '—'}
        />
        <KpiCard
          loading={perf.isPending}
          label={<GlossaryTerm term="sharpe" />}
          value={p?.sharpe != null ? formatNumber(p.sharpe) : '—'}
        />
        <KpiCard
          loading={perf.isPending}
          label={<GlossaryTerm term="expectativa">Expectativa (R)</GlossaryTerm>}
          value={p?.expectancy_r != null ? formatSigned(p.expectancy_r, 2) : '—'}
          tone={p?.expectancy_r == null ? 'neutral' : p.expectancy_r >= 0 ? 'gain' : 'loss'}
        />
        <KpiCard
          loading={perf.isPending}
          label="Retorno no período"
          value={p?.total_return_pct != null ? formatPercent(p.total_return_pct, 2, true) : '—'}
          tone={p?.total_return_pct == null ? 'neutral' : p.total_return_pct >= 0 ? 'gain' : 'loss'}
          delta={p ? `${formatSigned(p.total_pnl)} USDT` : undefined}
        />
        <KpiCard
          loading={perf.isPending}
          label="Tempo parado em USDT"
          value={p?.idle_pct != null ? formatPercent(p.idle_pct, 0) : '—'}
          delta="não é falha: esperar é decisão"
          glyph={false}
        />
      </section>

      {none ? (
        <EmptyState icon={ChartLine} title="Ainda sem dados suficientes">
          Os gráficos aparecem após a primeira operação encerrada. Até lá, ficar parado em USDT é
          uma decisão válida.
        </EmptyState>
      ) : (
        <>
          <section className="grid gap-4 lg:grid-cols-2">
            <Card aria-labelledby="eq-t">
              <CardHeader>
                <CardTitle id="eq-t">
                  <ChartLine className="size-5" aria-hidden="true" /> Curva de capital
                </CardTitle>
                <span className="text-sec text-muted">USDT</span>
              </CardHeader>
              {equity.data && equity.data.points.length > 1 ? (
                <EquityChart series={equity.data} />
              ) : equity.data ? (
                <p className="text-sec text-muted">
                  A curva aparece após o primeiro dia de registro.
                </p>
              ) : equity.isError ? (
                <CardError error={equity.error} onRetry={retry(KEYS.equity)} />
              ) : (
                <Skeleton className="h-[220px]" aria-busy="true" />
              )}
            </Card>

            <Card aria-labelledby="hist-t">
              <CardHeader>
                <CardTitle id="hist-t">
                  Distribuição dos resultados em <GlossaryTerm term="r" />
                </CardTitle>
              </CardHeader>
              {trades.isError ? (
                <CardError error={trades.error} onRetry={retry(KEYS.trades)} />
              ) : trades.isPending ? (
                <Skeleton className="h-[240px]" aria-busy="true" />
              ) : rValues.length === 0 ? (
                <p className="text-sec text-muted">
                  O histograma aparece após a primeira operação encerrada.
                </p>
              ) : (
                <>
                  <RHistogram values={rValues} />
                  {histogramTruncated ? (
                    <p className="text-cap text-muted">Considera as 200 operações mais recentes.</p>
                  ) : null}
                </>
              )}
            </Card>
          </section>

          <section className="grid gap-4 lg:grid-cols-2">
            {groups('Resultado por estratégia', p?.by_strategy, 'Estratégia')}
            {groups('Resultado por par', p?.by_pair, 'Par')}
            {groups('Resultado por mês', p?.by_month, 'Mês', monthLabel)}
            <Card aria-labelledby="reg-t">
              <CardHeader>
                <CardTitle id="reg-t">
                  Resultado por <GlossaryTerm term="regime" />
                </CardTitle>
              </CardHeader>
              <p className="text-sec text-muted">
                Ainda não disponível: o robô não registra o regime de mercado em cada operação.
                Quando passar a registrar, a divisão aparece aqui.
              </p>
            </Card>
          </section>
        </>
      )}

      <Card aria-labelledby="ev-t">
        <CardHeader>
          <CardTitle id="ev-t">Operações evitadas</CardTitle>
        </CardHeader>
        {p ? (
          p.avoided.length > 0 ? (
            <ul className="flex flex-col gap-1 text-sec">
              {p.avoided.map((a) => (
                <li key={a.code} className="tabular-nums">
                  <strong>{a.count}</strong> × {a.label}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sec text-muted">
              Nenhuma oportunidade recusada neste período
              {p.signals_refused > 0 ? ` (${p.signals_refused} recusas no total)` : ''}.
            </p>
          )
        ) : (
          skeletons(2, 'h-5')
        )}
        {p && p.notes.length > 0 ? (
          <ul className="list-disc pl-5 text-cap text-muted">
            {p.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        ) : null}
      </Card>
    </>
  );
}
