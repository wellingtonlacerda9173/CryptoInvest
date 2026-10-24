import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeftRight } from 'lucide-react';
import { Tabs } from 'radix-ui';
import { Link } from 'react-router-dom';
import { KEYS, usePositions } from '@/api/queries';
import { CardError } from '@/components/kit/CardError';
import { EmptyState } from '@/components/kit/EmptyState';
import { PositionsTable } from '@/components/kit/PositionsTable';
import { SimulatedTag } from '@/components/kit/SimulatedTag';
import { PageHeader } from '@/components/layout/PageHeader';
import { buttonVariants } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';
import { ClosedTrades } from './operacoes/ClosedTrades';

const TRIGGER =
  'min-h-11 cursor-pointer border-b-[3px] border-transparent bg-transparent px-4 font-semibold text-muted data-[state=active]:border-info data-[state=active]:text-text';

export default function OperacoesPage() {
  const qc = useQueryClient();
  const positions = usePositions();
  const open = positions.data ?? [];
  const simulated = open.length === 0 || open.every((p) => p.mode !== 'live');

  return (
    <>
      <PageHeader
        title="Operações"
        description="Posições abertas e histórico, em R e em USDT. Perdas sempre aparecem."
      />
      <Card aria-labelledby="ops-t">
        <CardHeader>
          <CardTitle id="ops-t">
            <ArrowLeftRight className="size-5" aria-hidden="true" /> Suas operações{' '}
            {simulated ? <SimulatedTag /> : null}
          </CardTitle>
        </CardHeader>
        <Tabs.Root defaultValue="abertas" className="flex flex-col gap-4">
          <Tabs.List aria-label="Tipo de operação" className="flex border-b border-border">
            <Tabs.Trigger value="abertas" className={cn(TRIGGER)}>
              Abertas ({positions.data ? open.length : '…'})
            </Tabs.Trigger>
            <Tabs.Trigger value="encerradas" className={cn(TRIGGER)}>
              Encerradas
            </Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="abertas">
            {positions.isError ? (
              <CardError
                error={positions.error}
                onRetry={() => void qc.invalidateQueries({ queryKey: KEYS.positions })}
                retrying={positions.isFetching}
              />
            ) : !positions.data ? (
              <Skeleton className="h-24" aria-busy="true" />
            ) : open.length > 0 ? (
              <PositionsTable positions={open} />
            ) : (
              <EmptyState
                title="Nenhuma operação aberta"
                action={
                  <Link to="/decisoes" className={buttonVariants()}>
                    Ver decisões ao vivo
                  </Link>
                }
              >
                Ficar parado em USDT é uma decisão válida. O robô só entra quando surge uma boa
                oportunidade.
              </EmptyState>
            )}
          </Tabs.Content>
          <Tabs.Content value="encerradas">
            <ClosedTrades />
          </Tabs.Content>
        </Tabs.Root>
      </Card>
    </>
  );
}
