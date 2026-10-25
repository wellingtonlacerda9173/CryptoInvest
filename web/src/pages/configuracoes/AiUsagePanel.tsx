import { Bot, ShieldCheck } from 'lucide-react';
import { useAiUsage } from '@/api/aiQueries';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { LimitBar } from '@/components/kit/LimitBar';
import { Skeleton } from '@/components/ui/skeleton';
import { formatNumber } from '@/lib/format';

const usd = (v: number) => `US$ ${formatNumber(v, 2)}`;

export function AiUsagePanel() {
  const usage = useAiUsage();
  const u = usage.data;
  const month = u
    ? new Intl.DateTimeFormat('pt-BR', { month: 'long', timeZone: 'UTC' }).format(
        new Date(u.month_start),
      )
    : '';

  return (
    <section
      aria-labelledby="ai-use-t"
      className="flex flex-col gap-3 rounded-xl border border-border bg-surface-2 px-4 py-3.5"
    >
      <h3 id="ai-use-t" className="flex items-center gap-2 font-bold">
        <Bot className="size-5" aria-hidden="true" /> Uso da IA no mês
      </h3>
      <p role="note" className="flex items-start gap-2 text-sec">
        <ShieldCheck className="mt-0.5 size-4 shrink-0 text-info" aria-hidden="true" />
        <span>
          <strong>A IA só veta ou reduz a confiança.</strong> Ela nunca aumenta o risco, o tamanho
          da operação, o stop ou o alvo, e nunca cria ordens.
        </span>
      </p>
      {usage.isError ? (
        <CardError
          error={usage.error}
          onRetry={() => void usage.refetch()}
          retrying={usage.isFetching}
        />
      ) : !u ? (
        <div aria-busy="true" className="flex flex-col gap-2">
          <Skeleton className="h-8" />
          <Skeleton className="h-8" />
        </div>
      ) : (
        <>
          {u.budget_usd !== null && u.budget_usd > 0 ? (
            <LimitBar
              name={`Gasto estimado em ${month}`}
              label="Gasto da IA no mês"
              valueText={`${usd(u.spent_usd)} de ${usd(u.budget_usd)}`}
              value={u.spent_usd}
              limit={u.budget_usd}
              note="Ao chegar no teto, a IA para de ser consultada e o robô segue só com as regras."
            />
          ) : (
            <p className="tabular-nums">
              <strong>Gasto estimado em {month}:</strong> {usd(u.spent_usd)}
              {u.budget_usd === 0 ? ' (orçamento zerado: a IA não é consultada)' : ''}
            </p>
          )}
          <ul className="flex flex-wrap items-center gap-2" aria-label="Estado da IA">
            <li>
              <Chip tone={u.runner_status === 'ligada' ? 'ok' : 'off'} icon={false}>
                {u.runner_status ? `Robô: IA ${u.runner_status}` : 'Robô: sem informação'}
              </Chip>
            </li>
            <li>
              {u.circuit_open === null ? (
                <Chip tone="off" icon={false}>
                  Circuito: sem informação
                </Chip>
              ) : u.circuit_open ? (
                <Chip tone="warn">Circuito aberto: IA pausada por falhas seguidas</Chip>
              ) : (
                <Chip tone="ok">Circuito fechado: IA respondendo</Chip>
              )}
            </li>
            <li className="text-sec text-muted tabular-nums">
              {u.calls} chamadas · {u.failures} falhas · {u.budget_stops} pedidos barrados por
              orçamento
            </li>
          </ul>
        </>
      )}
    </section>
  );
}
