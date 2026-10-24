import { useQueryClient } from '@tanstack/react-query';
import { ChartCandlestick, CircleCheck, TriangleAlert, WifiOff } from 'lucide-react';
import { KEYS } from '@/api/queries';
import { TIMEFRAMES, type Timeframe } from '@/api/marketSchemas';
import { CandleChart } from '@/components/kit/CandleChart';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { EmptyState } from '@/components/kit/EmptyState';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { SimulatedTag } from '@/components/kit/SimulatedTag';
import { PageHeader } from '@/components/layout/PageHeader';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { LabeledSelect } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { directionOf, formatClock, formatNumber, formatPercent, formatSigned } from '@/lib/format';
import { cn } from '@/lib/utils';
import { useMercado } from './mercado/useMercado';

const TF_LABEL: Record<Timeframe, string> = {
  '1m': '1 minuto',
  '5m': '5 minutos',
  '15m': '15 minutos',
  '30m': '30 minutos',
  '1h': '1 hora',
  '4h': '4 horas',
  '1d': '1 dia',
};

const stamp = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso));

export default function MercadoPage() {
  const qc = useQueryClient();
  const m = useMercado();
  const { candles } = m;
  const data = candles.data;
  const dir = directionOf(data?.change_24h_pct ?? 0);

  return (
    <>
      <PageHeader
        title="Mercado"
        description="O que o robô vê: velas, barras elefante e as operações em andamento."
      />

      <Card aria-labelledby="graf-t">
        <CardHeader>
          <CardTitle id="graf-t">
            <ChartCandlestick className="size-5" aria-hidden="true" /> {m.symbol ?? 'Gráfico'}{' '}
            {data?.simulated ? <SimulatedTag /> : null}
          </CardTitle>
          <div className="flex flex-wrap items-end gap-3">
            <LabeledSelect
              label="Par"
              value={m.symbol ?? ''}
              onChange={(e) => m.setSymbol(e.target.value)}
              disabled={m.symbols.length === 0}
            >
              {m.symbols.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </LabeledSelect>
            <LabeledSelect
              label="Tempo de cada vela"
              value={m.timeframe}
              onChange={(e) => m.setTimeframe(e.target.value as Timeframe)}
            >
              {TIMEFRAMES.map((t) => (
                <option key={t} value={t}>
                  {TF_LABEL[t]}
                </option>
              ))}
            </LabeledSelect>
          </div>
        </CardHeader>

        {m.disconnected ? (
          <div
            role="status"
            className="flex items-center gap-2 rounded-xl border border-warn bg-warn-bg px-3.5 py-2.5 text-sec"
          >
            <WifiOff className="size-4 shrink-0 text-warn" aria-hidden="true" />
            Sem conexão em tempo real. O gráfico mostra os últimos dados recebidos
            {candles.dataUpdatedAt ? ` (às ${formatClock(candles.dataUpdatedAt)})` : ''}.
          </div>
        ) : null}

        {data?.blind_since ? (
          <div
            role="alert"
            className="flex items-center gap-2 rounded-xl border border-loss bg-loss-bg px-3.5 py-2.5 text-sec"
          >
            <TriangleAlert className="size-4 shrink-0 text-loss" aria-hidden="true" />
            <span>
              Sem preços desde {stamp(data.blind_since)}. O robô não vê o mercado agora e não abre
              novas entradas; o stop segue ativo na corretora.
            </span>
          </div>
        ) : null}

        {data ? (
          <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
            <span className="text-kpi font-bold tabular-nums">
              {data.last_price !== null ? formatNumber(data.last_price) : '—'}
            </span>
            {data.change_24h_pct !== null ? (
              <span
                className={cn(
                  'font-semibold tabular-nums',
                  dir === 'up' ? 'text-gain' : dir === 'down' ? 'text-loss' : 'text-muted',
                )}
              >
                <span aria-hidden="true">{dir === 'up' ? '▲ ' : dir === 'down' ? '▼ ' : ''}</span>
                {formatPercent(data.change_24h_pct, 2, true)} em 24 h
              </span>
            ) : null}
            {data.volume_24h !== null ? (
              <span className="text-sec text-muted tabular-nums">
                Volume 24 h: {formatNumber(data.volume_24h, 0)}
              </span>
            ) : null}
            <span className="text-sec text-muted">
              Atualizado às {formatClock(candles.dataUpdatedAt)}
            </span>
          </div>
        ) : null}

        <div className="flex flex-wrap gap-x-6 gap-y-2" role="group" aria-label="O que mostrar">
          <label className="flex items-center gap-2 text-sec">
            <Checkbox
              checked={m.showElephants}
              onCheckedChange={(v) => m.setShowElephants(v === true)}
            />
            <GlossaryTerm term="barra-elefante">Barras elefante</GlossaryTerm>
          </label>
          <label className="flex items-center gap-2 text-sec">
            <Checkbox checked={m.showLevels} onCheckedChange={(v) => m.setShowLevels(v === true)} />
            Stops e alvos
          </label>
        </div>

        {candles.isError ? (
          <CardError
            error={candles.error}
            onRetry={() => void qc.invalidateQueries({ queryKey: KEYS.candles })}
            retrying={candles.isFetching}
            updatedAt={candles.dataUpdatedAt || undefined}
          />
        ) : !m.symbol || candles.isPending ? (
          <Skeleton className="h-[380px]" aria-busy="true" />
        ) : data && data.candles.length === 0 ? (
          <EmptyState icon={ChartCandlestick} title="Ainda sem velas para este par">
            O histórico deste par está sendo carregado. Escolha outro par ou volte em alguns
            minutos.
          </EmptyState>
        ) : data ? (
          <>
            <CandleChart
              candles={data.candles}
              markers={data.markers}
              levels={m.levels}
              showElephants={m.showElephants}
              label={m.summary}
            />
            <ul className="flex flex-wrap gap-x-4 gap-y-1 text-cap text-muted">
              <li>▲ Entrada</li>
              <li>▼ Saída</li>
              <li>■ Stop</li>
              <li>● Alvo</li>
              <li>E Barra elefante (contorno âmbar)</li>
              <li>Vela vazada: fechou em alta. Vela cheia: fechou em baixa.</li>
            </ul>
          </>
        ) : null}
      </Card>

      <section className="grid gap-4 lg:grid-cols-2">
        <Card aria-labelledby="ve-t">
          <CardHeader>
            <CardTitle id="ve-t">O que o robô vê em {m.symbol ?? 'este par'}</CardTitle>
          </CardHeader>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sec">
            <dt className="text-muted">Última decisão</dt>
            <dd>{data?.last_decision ?? 'Nenhuma ainda'}</dd>
            <dt className="text-muted">Barras elefante</dt>
            <dd>
              {data
                ? `${data.markers.filter((k) => k.kind === 'elephant').length} no período mostrado`
                : '—'}
            </dd>
            <dt className="text-muted">Posição</dt>
            <dd>
              {m.position ? (
                <span className="inline-flex flex-wrap items-center gap-2">
                  <Chip tone="ok" icon={CircleCheck}>
                    Comprado
                  </Chip>
                  <span className="tabular-nums">
                    entrada {formatNumber(m.position.entry_price)} · stop{' '}
                    {formatNumber(m.position.stop)}
                    {m.position.unrealized_pnl !== null
                      ? ` · ${formatSigned(m.position.unrealized_pnl)} USDT`
                      : ''}
                  </span>
                </span>
              ) : (
                'Parado em USDT neste par. Ficar fora também é uma decisão válida.'
              )}
            </dd>
          </dl>
        </Card>

        <Card aria-labelledby="tab-t">
          <CardHeader>
            <CardTitle id="tab-t">Últimas velas</CardTitle>
          </CardHeader>
          {data && data.candles.length > 0 ? (
            <div
              tabIndex={0}
              role="region"
              aria-label="Tabela das últimas velas"
              className="max-h-[260px] overflow-auto"
            >
              <table className="w-full min-w-[420px] border-collapse text-left text-sec tabular-nums">
                <thead>
                  <tr className="text-muted">
                    {['Início', 'Abertura', 'Máxima', 'Mínima', 'Fechamento'].map((h) => (
                      <th key={h} scope="col" className="border-b border-border px-2 py-1.5">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {[...data.candles]
                    .slice(-20)
                    .reverse()
                    .map((k) => (
                      <tr key={k.t}>
                        <th scope="row" className="px-2 py-1.5 font-normal">
                          {stamp(k.t)}
                        </th>
                        <td className="px-2 py-1.5">{formatNumber(k.o)}</td>
                        <td className="px-2 py-1.5">{formatNumber(k.h)}</td>
                        <td className="px-2 py-1.5">{formatNumber(k.l)}</td>
                        <td className="px-2 py-1.5">{formatNumber(k.c)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sec text-muted">As velas aparecem aqui quando houver histórico.</p>
          )}
        </Card>
      </section>
    </>
  );
}
