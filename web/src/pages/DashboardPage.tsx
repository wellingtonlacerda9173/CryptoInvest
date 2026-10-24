import { useQueryClient } from '@tanstack/react-query';
import { AlignLeft, ArrowLeftRight, ChartLine, CircleCheck, Lock, ShieldCheck } from 'lucide-react';
import { lazy, Suspense } from 'react';
import { Link } from 'react-router-dom';
import {
  KEYS,
  useAccount,
  useBotStatus,
  useDecisions,
  useEquity,
  useLiveGate,
  usePositions,
} from '@/api/queries';
import type { Role } from '@/api/schemas';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { DecisionsFeed } from '@/components/kit/DecisionsFeed';
import { EmptyState } from '@/components/kit/EmptyState';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { IneligibleSeal } from '@/components/kit/IneligibleSeal';
import { KpiCard } from '@/components/kit/KpiCard';
import { LiveCriteria } from '@/components/kit/LiveCriteria';
import { ModeBadge } from '@/components/kit/ModeBadge';
import { PositionsTable } from '@/components/kit/PositionsTable';
import { RiskLimitsList } from '@/components/kit/RiskLimitsList';
import { SimulatedTag } from '@/components/kit/SimulatedTag';
import { StateHero } from '@/components/kit/StateHero';
import { PageHeader } from '@/components/layout/PageHeader';
import { buttonVariants } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useAuth } from '@/auth/AuthContext';
import { directionOf, formatNumber, formatPercent, formatSigned } from '@/lib/format';
import { MODE_META } from '@/lib/modes';
import { OnboardingNudge } from './dashboard/OnboardingNudge';

const HERO_SKELETON = <Skeleton className="h-[92px] rounded-card" aria-hidden="true" />;

// recharts pesa ~250 kB: so e baixado quando a curva aparece, nao no pacote principal.
const EquityChart = lazy(() =>
  import('@/components/kit/EquityChart').then((m) => ({ default: m.EquityChart })),
);

export default function DashboardPage() {
  const qc = useQueryClient();
  const { session } = useAuth();
  const status = useBotStatus();
  const account = useAccount();
  const equity = useEquity(30);
  const decisions = useDecisions(5);
  const positions = usePositions();
  const gate = useLiveGate();
  const s = status.data;
  const a = account.data;
  const simulated = s?.simulated ?? a?.simulated ?? true;
  const retry = (key: readonly string[]) => () => void qc.invalidateQueries({ queryKey: key });
  const maxPositions = s?.limits.find((l) => l.key === 'open_positions')?.limit;
  const role: Role | undefined = session?.user.role;
  const dayDir = directionOf(a?.day_pnl ?? 0);
  const totalDir = directionOf(a?.total_pnl ?? 0);
  const equityLabel = `Patrimônio${simulated ? ' (simulado)' : ' (real)'}`;
  const openPositions = positions.data ?? [];

  return (
    <>
      <PageHeader title="Dashboard" description="Visão geral do robô, em um olhar." />
      {role === 'admin' ? <OnboardingNudge /> : null}

      {s ? (
        <StateHero status={s} page="dashboard" />
      ) : status.isError ? (
        <CardError
          error={status.error}
          onRetry={retry(KEYS.status)}
          retrying={status.isFetching}
          updatedAt={status.dataUpdatedAt || undefined}
        />
      ) : (
        HERO_SKELETON
      )}

      <section
        aria-label="Indicadores principais"
        className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4"
      >
        <KpiCard
          loading={account.isPending}
          label={equityLabel}
          value={a?.equity != null ? formatNumber(a.equity) : '—'}
          unit="USDT"
          tone={totalDir === 'up' ? 'gain' : totalDir === 'down' ? 'loss' : 'neutral'}
          delta={
            a?.total_pnl_pct != null
              ? `${formatPercent(a.total_pnl_pct, 2, true)} desde o início`
              : undefined
          }
        />
        <KpiCard
          loading={account.isPending}
          label="Resultado do dia"
          value={a?.day_pnl != null ? formatSigned(a.day_pnl) : '—'}
          unit="USDT"
          tone={dayDir === 'up' ? 'gain' : dayDir === 'down' ? 'loss' : 'neutral'}
          delta={a?.day_pnl_pct != null ? formatPercent(a.day_pnl_pct, 2, true) : undefined}
        />
        <KpiCard
          loading={status.isPending}
          label={<GlossaryTerm term="drawdown">Drawdown atual</GlossaryTerm>}
          value={s?.drawdown_pct != null ? formatPercent(s.drawdown_pct, 1) : '—'}
          delta={s?.peak_equity != null ? `pico em ${formatNumber(s.peak_equity)}` : undefined}
        />
        <KpiCard
          loading={status.isPending}
          label="Operações abertas"
          value={s ? s.open_positions.length : undefined}
          unit={maxPositions ? `de ${maxPositions}` : undefined}
          delta={s?.open_positions.length ? s.open_positions.join(', ') : undefined}
        />
      </section>

      <section className="grid gap-4 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
        <Card aria-labelledby="travas-t">
          <CardHeader>
            <CardTitle id="travas-t">
              <ShieldCheck className="size-5" aria-hidden="true" /> Travas de risco
            </CardTitle>
            {s && s.limits.every((l) => l.level === 'ok') ? (
              <Chip tone="ok" icon={CircleCheck}>
                Nenhuma trava ativada
              </Chip>
            ) : null}
          </CardHeader>
          {s ? (
            <RiskLimitsList limits={s.limits} />
          ) : status.isError ? (
            <CardError error={status.error} onRetry={retry(KEYS.status)} />
          ) : (
            <div className="flex flex-col gap-4" aria-busy="true">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-16" />
              ))}
            </div>
          )}
        </Card>

        <Card aria-labelledby="live-t">
          <CardHeader>
            <CardTitle id="live-t">
              <Lock className="size-5" aria-hidden="true" /> Caminho para o Live
            </CardTitle>
            {s ? <ModeBadge mode={s.mode} hintOnMobile={false} /> : null}
          </CardHeader>
          {gate.data ? (
            <>
              {s?.allow_open_calendar || gate.data.ineligible_reason ? (
                <IneligibleSeal
                  reason={s?.allow_open_calendar ? null : gate.data.ineligible_reason}
                />
              ) : null}
              <LiveCriteria criteria={gate.data.criteria} />
              <Link to="/controle" className={buttonVariants()}>
                Ver detalhes no Controle
              </Link>
            </>
          ) : gate.isError ? (
            <CardError error={gate.error} onRetry={retry(KEYS.liveGate)} />
          ) : (
            <div className="flex flex-col gap-3" aria-busy="true">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-6" />
              ))}
            </div>
          )}
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Card aria-labelledby="curva-t">
          <CardHeader>
            <CardTitle id="curva-t">
              <ChartLine className="size-5" aria-hidden="true" /> Curva de capital{' '}
              {equity.data?.simulated ? <SimulatedTag /> : null}
            </CardTitle>
            <span className="text-sec text-muted">últimos 30 dias, USDT</span>
          </CardHeader>
          {equity.data && equity.data.points.length > 1 ? (
            <Suspense fallback={<Skeleton className="h-[180px] sm:h-[220px]" aria-busy="true" />}>
              <EquityChart series={equity.data} />
            </Suspense>
          ) : equity.data ? (
            <EmptyState icon={ChartLine} title="A curva aparece após o primeiro dia">
              Assim que o robô registrar o saldo por mais de um dia, o gráfico começa a ser
              desenhado.
            </EmptyState>
          ) : equity.isError ? (
            <CardError error={equity.error} onRetry={retry(KEYS.equity)} />
          ) : (
            <Skeleton className="h-[220px]" aria-busy="true" />
          )}
        </Card>

        <Card aria-labelledby="dec-t">
          <CardHeader>
            <CardTitle id="dec-t">
              <AlignLeft className="size-5" aria-hidden="true" /> Últimas decisões
            </CardTitle>
            <Link to="/decisoes" className="text-sec font-semibold underline underline-offset-2">
              Ver todas
            </Link>
          </CardHeader>
          {decisions.data && decisions.data.items.length > 0 ? (
            <DecisionsFeed items={decisions.data.items} />
          ) : decisions.data ? (
            <EmptyState icon={AlignLeft} title="Ainda nenhuma decisão">
              O robô decide a cada vela fechada (a cada 1 hora). Esperar também é uma decisão.
            </EmptyState>
          ) : decisions.isError ? (
            <CardError error={decisions.error} onRetry={retry(KEYS.decisions)} />
          ) : (
            <div className="flex flex-col gap-3" aria-busy="true">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-12" />
              ))}
            </div>
          )}
        </Card>
      </section>

      <Card aria-labelledby="pos-t">
        <CardHeader>
          <CardTitle id="pos-t">
            <ArrowLeftRight className="size-5" aria-hidden="true" /> Posição aberta{' '}
            {simulated ? <SimulatedTag>SIMULADA</SimulatedTag> : null}
          </CardTitle>
          {s ? (
            <span className="text-sec text-muted">
              {simulated ? 'stop sempre ativo na corretora simulada' : MODE_META[s.mode].hint}
            </span>
          ) : null}
        </CardHeader>
        {positions.data ? (
          openPositions.length > 0 ? (
            <PositionsTable positions={openPositions} />
          ) : (
            <EmptyState title="Nenhuma operação ainda">
              O robô só opera quando surge uma boa oportunidade. Ficar parado é normal.
            </EmptyState>
          )
        ) : positions.isError ? (
          <CardError error={positions.error} onRetry={retry(KEYS.positions)} />
        ) : (
          <Skeleton className="h-16" aria-busy="true" />
        )}
      </Card>
    </>
  );
}
