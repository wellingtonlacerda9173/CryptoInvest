import { Scale, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { useAiValue } from '@/api/aiQueries';
import type { AiValue } from '@/api/aiSchemas';
import { CardError } from '@/components/kit/CardError';
import { EmptyState } from '@/components/kit/EmptyState';
import { KpiCard } from '@/components/kit/KpiCard';
import { LabeledSelect } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { formatNumber, formatPercent, formatSigned } from '@/lib/format';

const PERIODS = [
  [7, 'Últimos 7 dias'],
  [30, 'Últimos 30 dias'],
  [90, 'Últimos 90 dias'],
] as const;

const usd = (v: number) => `US$ ${formatNumber(v, 2)}`;
const usdSigned = (v: number) => `US$ ${formatSigned(v, 2)}`;
const stamp = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit' }).format(new Date(iso));

const STATUS_LABEL: Record<string, string> = {
  stop: 'teria batido o stop',
  target: 'teria batido o alvo',
  timeout: 'teria saído por prazo',
};

function Verdict({ v }: { v: AiValue }) {
  const warn =
    v.small_sample || v.verdict === 'no_conclusion' || v.verdict === 'insufficient_sample';
  return (
    <p
      role="note"
      data-verdict={v.verdict}
      className={
        warn
          ? 'flex items-start gap-2 rounded-xl border border-warn bg-warn-bg px-3.5 py-2.5'
          : 'flex items-start gap-2 rounded-xl border border-info bg-info-bg px-3.5 py-2.5'
      }
    >
      <TriangleAlert className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
      <span>
        <strong>Veredito: </strong>
        {v.verdict_text}
        {v.small_sample && v.verdict !== 'no_activity' ? (
          <> Com poucos vetos o resultado pode ser só acaso; não tire conclusões ainda.</>
        ) : null}
      </span>
    </p>
  );
}

function Report({ v }: { v: AiValue }) {
  if (v.verdict === 'no_activity')
    return (
      <EmptyState icon={Scale} title="Sem atividade da IA neste período">
        A IA está desligada ou ainda não analisou nenhum sinal. Quando houver vetos, este relatório
        compara o que eles teriam rendido com o que foi executado.
      </EmptyState>
    );
  return (
    <>
      <Verdict v={v} />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
        <KpiCard label="Sinais vetados" value={v.vetoes} delta={`${v.simulated} simulados`} />
        <KpiCard
          label="Falsos vetos"
          value={v.false_vetoes}
          delta={
            v.false_veto_rate === null
              ? 'sem vetos simulados'
              : `${formatPercent(v.false_veto_rate * 100, 0)} dos simulados`
          }
          tone={v.false_vetoes > 0 ? 'warn' : 'neutral'}
          glyph={false}
        />
        <KpiCard
          label="Perdas evitadas"
          value={usd(v.avoided_loss)}
          delta="simulação"
          tone="gain"
          glyph={false}
        />
        <KpiCard
          label="Ganhos barrados"
          value={usd(v.missed_gain)}
          delta="simulação"
          tone="loss"
          glyph={false}
        />
        <KpiCard label="Custo da IA" value={usd(v.cost_usd)} delta={`${v.calls} chamadas`} />
        <KpiCard
          label="Valor líquido"
          value={usdSigned(v.net_value_usd)}
          delta="vetos menos custo"
          tone={v.net_value_usd > 0 ? 'gain' : v.net_value_usd < 0 ? 'loss' : 'neutral'}
        />
      </div>
      <section aria-label="Entradas aprovadas pela IA" className="flex flex-col gap-1">
        <h3 className="font-bold">Entradas que a IA aprovou e foram executadas</h3>
        <p className="tabular-nums text-sec">
          {v.approved_trades} operações: {v.approved_wins} ganhos e{' '}
          <strong>{v.approved_losses} perdas</strong> (perdas somam {usdSigned(v.approved_loss_pnl)}
          ); resultado {usdSigned(v.approved_pnl)}, {formatSigned(v.approved_total_r)} R.
        </p>
      </section>
      <p className="text-sec text-muted">
        {v.not_filled} vetos não chegariam a entrar, {v.unresolved} ainda estão em aberto e{' '}
        {v.not_simulated} não tinham dados para simular. {v.failed_calls} de {v.calls} chamadas
        falharam. Ganho e perda evitados são estimativas de uma simulação pessimista, não dinheiro
        real.
      </p>
      {v.cases.length > 0 ? (
        <section aria-label="Vetos simulados" className="flex flex-col gap-1">
          <h3 className="font-bold">Últimos vetos simulados</h3>
          <ul className="flex flex-col gap-0.5 text-sec tabular-nums">
            {v.cases.slice(0, 10).map((c) => (
              <li key={`${c.decided_at}|${c.symbol}|${c.strategy}`}>
                {stamp(c.decided_at)} {c.symbol} ({c.strategy}):{' '}
                {STATUS_LABEL[c.status] ?? c.status}, {usdSigned(c.pnl)} (
                {formatSigned(c.r_multiple)} R)
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  );
}

export function AiValueTab() {
  const [days, setDays] = useState<number>(30);
  const value = useAiValue(days);

  return (
    <section aria-labelledby="val-t" className="flex flex-col gap-3.5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h3 id="val-t" className="flex items-center gap-2 text-card font-bold">
          <Scale className="size-5" aria-hidden="true" /> Valor da IA
        </h3>
        <LabeledSelect
          label="Período"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
        >
          {PERIODS.map(([d, label]) => (
            <option key={d} value={d}>
              {label}
            </option>
          ))}
        </LabeledSelect>
      </div>
      {value.isError ? (
        <CardError
          error={value.error}
          onRetry={() => void value.refetch()}
          retrying={value.isFetching}
        />
      ) : !value.data ? (
        <div aria-busy="true" className="flex flex-col gap-3">
          <Skeleton className="h-14" />
          <Skeleton className="h-32" />
        </div>
      ) : (
        <Report v={value.data} />
      )}
    </section>
  );
}
