import { History as HistoryIcon } from 'lucide-react';
import { useBacktestHistory } from '@/api/backtestQueries';
import type { BacktestJob, JobStatus } from '@/api/backtestSchemas';
import { CardError } from '@/components/kit/CardError';
import { Chip, type ChipTone } from '@/components/kit/Chip';
import { EmptyState } from '@/components/kit/EmptyState';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

const STATUS: Record<JobStatus, { label: string; tone: ChipTone }> = {
  queued: { label: 'Na fila', tone: 'off' },
  running: { label: 'Rodando', tone: 'info' },
  succeeded: { label: 'Concluído', tone: 'info' },
  failed: { label: 'Falhou', tone: 'bad' },
  cancelled: { label: 'Cancelado', tone: 'off' },
};

const when = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso));

const day = (v: unknown) =>
  typeof v === 'string' ? v.slice(0, 10).split('-').reverse().join('/') : '?';

function summary(j: BacktestJob) {
  const symbols = Array.isArray(j.params.symbols) ? (j.params.symbols as string[]).join(', ') : '—';
  return `${symbols} · ${String(j.params.timeframe ?? '')} · ${day(j.params.start)} a ${day(j.params.end)}`;
}

export function History({ onOpen }: { onOpen: (id: string) => void }) {
  const history = useBacktestHistory();
  const items = history.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <Card aria-labelledby="hist-t">
      <CardHeader>
        <CardTitle id="hist-t">
          <HistoryIcon className="size-5" aria-hidden="true" /> Backtests anteriores
        </CardTitle>
      </CardHeader>
      {history.isError ? (
        <CardError
          error={history.error}
          onRetry={() => void history.refetch()}
          retrying={history.isFetching}
        />
      ) : history.isPending ? (
        <Skeleton className="h-16" aria-busy="true" />
      ) : items.length === 0 ? (
        <EmptyState title="Nenhum backtest ainda">
          Rode seu primeiro backtest em poucos passos.
        </EmptyState>
      ) : (
        <>
          <ul className="flex flex-col">
            {items.map((j) => (
              <li
                key={j.id}
                className="flex flex-wrap items-center justify-between gap-3 border-b border-border py-3 first:pt-0 last:border-0 last:pb-0"
              >
                <div className="flex min-w-0 flex-col">
                  <span className="flex flex-wrap items-center gap-2 font-semibold">
                    {summary(j)}
                    <Chip tone={STATUS[j.status].tone}>{STATUS[j.status].label}</Chip>
                  </span>
                  <span className="text-sec text-muted">
                    {when(j.created_at)} por {j.created_by}
                    {j.status === 'running' ? ` · ${Math.round(j.progress.percent)}%` : ''}
                  </span>
                </div>
                <Button
                  aria-label={`${j.status === 'succeeded' ? 'Ver relatório' : 'Abrir'} do backtest de ${summary(j)}, criado em ${when(j.created_at)}`}
                  onClick={() => onOpen(j.id)}
                >
                  {j.status === 'succeeded' ? 'Ver relatório' : 'Abrir'}
                </Button>
              </li>
            ))}
          </ul>
          {history.hasNextPage ? (
            <div>
              <Button
                disabled={history.isFetchingNextPage}
                onClick={() => void history.fetchNextPage()}
              >
                Mostrar mais antigos
              </Button>
            </div>
          ) : null}
        </>
      )}
    </Card>
  );
}
