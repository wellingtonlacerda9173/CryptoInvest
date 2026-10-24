import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Ban, RotateCcw, XCircle } from 'lucide-react';
import { useState } from 'react';
import {
  backtestApi,
  BACKTEST_KEYS,
  useBacktestJob,
  useBacktestReport,
} from '@/api/backtestQueries';
import { ApiError } from '@/api/http';
import { CardError } from '@/components/kit/CardError';
import { ModeBadge } from '@/components/kit/ModeBadge';
import { notify } from '@/components/kit/Toaster';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Report } from './Report';

const STAGE: Record<string, string> = {
  loading: 'Baixando as velas',
  simulating: 'Simulando as operações',
  walk_forward: 'Conferindo em janelas (fora da amostra)',
  done: 'Concluindo',
};

export function JobView({
  jobId,
  canAct,
  onRedo,
  onBack,
}: {
  jobId: string;
  canAct: boolean;
  onRedo: (params: Record<string, unknown>) => void;
  onBack: () => void;
}) {
  const qc = useQueryClient();
  const job = useBacktestJob(jobId);
  const j = job.data;
  const report = useBacktestReport(jobId, j?.status === 'succeeded' && j.has_report);
  const [cancelling, setCancelling] = useState(false);

  const cancel = async () => {
    setCancelling(true);
    try {
      await backtestApi.cancel(jobId);
      await Promise.all([
        qc.invalidateQueries({ queryKey: BACKTEST_KEYS.job(jobId) }),
        qc.invalidateQueries({ queryKey: BACKTEST_KEYS.list }),
      ]);
    } catch (cause) {
      notify.error(
        'Não foi possível cancelar',
        cause instanceof ApiError && cause.problem.detail ? cause.problem.detail : 'Tente de novo.',
      );
    } finally {
      setCancelling(false);
    }
  };

  const back = (
    <Button onClick={onBack}>
      <ArrowLeft className="size-4" aria-hidden="true" /> Novo backtest e histórico
    </Button>
  );

  if (job.isError)
    return (
      <Card aria-labelledby="job-t">
        <CardTitle id="job-t">Backtest</CardTitle>
        <CardError error={job.error} onRetry={() => void job.refetch()} retrying={job.isFetching} />
        {back}
      </Card>
    );
  if (!j)
    return (
      <Card aria-labelledby="job-t" aria-busy="true">
        <CardTitle id="job-t">Backtest</CardTitle>
        <Skeleton className="h-16" />
      </Card>
    );

  const active = j.status === 'queued' || j.status === 'running';
  const percent = Math.max(0, Math.min(100, Math.round(j.progress.percent)));
  const pairs = Array.isArray(j.params.symbols) ? (j.params.symbols as string[]).join(', ') : '';

  return (
    <div className="flex flex-col gap-4">
      <Card aria-labelledby="job-t">
        <CardHeader>
          <CardTitle id="job-t">Backtest {pairs ? `de ${pairs}` : ''}</CardTitle>
          <ModeBadge mode="backtest" />
        </CardHeader>
        {active ? (
          <>
            <div
              role="progressbar"
              aria-label="Progresso do backtest"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
              className="h-3 w-full overflow-hidden rounded-pill border border-border-strong bg-surface-2"
            >
              <div
                className="h-full bg-backtest transition-[width] duration-300"
                style={{ width: `${percent}%` }}
              />
            </div>
            <p role="status" aria-live="polite" className="flex flex-wrap items-center gap-x-2">
              <strong>
                {j.status === 'queued' ? 'Na fila' : (STAGE[j.progress.stage] ?? 'Rodando')}
              </strong>
              <span className="tabular-nums">{percent}%</span>
              <span className="text-muted">
                {j.progress.symbol ? `${j.progress.symbol}. ` : ''}
                {j.progress.message}
              </span>
            </p>
            <p className="text-sec text-muted">
              O teste roda no servidor: pode fechar esta página e voltar depois pelo histórico.
            </p>
            <div className="flex flex-wrap items-center gap-2.5">
              <Button
                disabled={!canAct || cancelling || j.cancel_requested}
                onClick={() => void cancel()}
              >
                <Ban className="size-4" aria-hidden="true" />
                {j.cancel_requested || cancelling ? 'Cancelando…' : 'Cancelar o teste'}
              </Button>
              {canAct ? null : (
                <span className="text-sec text-muted">Só administradores cancelam backtests.</span>
              )}
            </div>
          </>
        ) : null}
        {j.status === 'failed' ? (
          <p role="alert" className="flex items-start gap-1.5 text-loss">
            <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              O teste não terminou.
              {j.error ? ` ${j.error}` : ''}
            </span>
          </p>
        ) : null}
        {j.status === 'cancelled' ? (
          <p role="status">Teste cancelado. Nada foi gravado no seu robô.</p>
        ) : null}
        {!active ? (
          <div className="flex flex-wrap gap-2.5">
            {back}
            {canAct ? (
              <Button onClick={() => onRedo(j.params)}>
                <RotateCcw className="size-4" aria-hidden="true" /> Refazer com outras premissas
              </Button>
            ) : null}
          </div>
        ) : null}
      </Card>

      {j.status === 'succeeded' ? (
        report.isError ? (
          <CardError error={report.error} onRetry={() => void report.refetch()} />
        ) : report.data ? (
          <Report report={report.data} />
        ) : (
          <Skeleton className="h-48" aria-busy="true" />
        )
      ) : null}
    </div>
  );
}
