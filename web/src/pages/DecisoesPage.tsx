import { useQueryClient } from '@tanstack/react-query';
import { AlignLeft, Radio, WifiOff } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { DECISION_FEED_MAX_PAGES, useDecisionFeed, useViewMode } from '@/api/marketQueries';
import { KEYS, useSettings } from '@/api/queries';
import type { Decision } from '@/api/schemas';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { EmptyState } from '@/components/kit/EmptyState';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { LabeledSelect } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useConnection } from '@/realtime/ConnectionProvider';
import { DecisionItem } from './decisoes/DecisionItem';

type Outcome = 'all' | 'traded' | 'not-traded';

export default function DecisoesPage() {
  const qc = useQueryClient();
  const mode = useViewMode();
  const { disconnected } = useConnection();
  const settings = useSettings();
  const [outcome, setOutcome] = useState<Outcome>('all');
  const [params] = useSearchParams();
  const [chosen, setSymbol] = useState(() => params.get('par') ?? '');
  const symbols = settings.data?.document.pairs.symbols ?? [];
  const symbol = symbols.includes(chosen) ? chosen : '';
  const filters = {
    ...(outcome === 'all' ? {} : { traded: outcome === 'traded' }),
    ...(symbol ? { symbol } : {}),
  };
  const feed = useDecisionFeed(mode, filters);

  const items = useMemo(() => {
    const seen = new Set<number>();
    const all: Decision[] = [];
    for (const page of feed.data?.pages ?? []) {
      for (const d of page.items) {
        if (!seen.has(d.id)) {
          seen.add(d.id);
          all.push(d);
        }
      }
    }
    return all;
  }, [feed.data]);
  const summary = feed.data?.pages[0]?.summary;
  const filtered = outcome !== 'all' || symbol !== '';
  const frozen = disconnected;

  return (
    <>
      <PageHeader
        title="Decisões ao vivo"
        description="Por que o robô entrou, esperou ou recusou, em palavras simples."
      />

      <Card aria-labelledby="dec-lista-t">
        <CardHeader>
          <CardTitle id="dec-lista-t">
            <AlignLeft className="size-5" aria-hidden="true" /> Linha do tempo
          </CardTitle>
          {frozen ? (
            <span className="inline-flex items-center gap-2">
              <Chip tone="warn" icon={WifiOff}>
                Pausado: sem conexão
              </Chip>
              <Button onClick={() => void feed.refetch()}>Retomar ao vivo</Button>
            </span>
          ) : (
            <Chip tone="ok" icon={Radio}>
              Ao vivo
            </Chip>
          )}
        </CardHeader>

        {summary ? (
          <p className="text-body" data-testid="resumo-dia">
            Hoje: {summary.signals} {summary.signals === 1 ? 'sinal' : 'sinais'} · {summary.entries}{' '}
            {summary.entries === 1 ? 'entrada' : 'entradas'} · {summary.refusals} recusas ·{' '}
            {summary.waits} esperas. Isso é normal: o robô só entra quando tudo se alinha.
          </p>
        ) : null}

        <div className="flex flex-wrap items-end gap-3" role="group" aria-label="Filtros">
          <LabeledSelect
            label="Mostrar"
            value={outcome}
            onChange={(e) => setOutcome(e.target.value as Outcome)}
          >
            <option value="all">Todas as decisões</option>
            <option value="traded">Operou</option>
            <option value="not-traded">Não operou</option>
          </LabeledSelect>
          <LabeledSelect label="Par" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            <option value="">Todos os pares</option>
            {symbols.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </LabeledSelect>
          {filtered ? (
            <Button
              onClick={() => {
                setOutcome('all');
                setSymbol('');
              }}
            >
              Limpar filtros
            </Button>
          ) : null}
        </div>

        {feed.isError ? (
          <CardError
            error={feed.error}
            onRetry={() => void qc.invalidateQueries({ queryKey: KEYS.decisions })}
            retrying={feed.isFetching}
            updatedAt={feed.dataUpdatedAt || undefined}
          />
        ) : feed.isPending ? (
          <div className="flex flex-col gap-3" aria-busy="true">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-14" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            icon={AlignLeft}
            title={filtered ? 'Nenhuma decisão com este filtro' : 'Ainda nenhuma decisão'}
          >
            {filtered
              ? 'Tente outro filtro ou limpe os filtros para ver tudo.'
              : 'O robô decide a cada vela fechada (a cada 1 hora). Esperar também é uma decisão.'}
          </EmptyState>
        ) : (
          <>
            <ul
              aria-label="Decisões do robô, da mais recente para a mais antiga"
              className="flex flex-col"
            >
              {items.map((d) => (
                <DecisionItem key={d.id} decision={d} />
              ))}
            </ul>
            {feed.hasNextPage && (feed.data?.pages.length ?? 0) >= DECISION_FEED_MAX_PAGES ? (
              <p className="text-center text-sec text-muted">
                Mostrando as decisões mais recentes. Use os filtros para ver outras.
              </p>
            ) : feed.hasNextPage ? (
              <Button onClick={() => void feed.fetchNextPage()} disabled={feed.isFetchingNextPage}>
                {feed.isFetchingNextPage ? 'Carregando…' : 'Carregar decisões anteriores'}
              </Button>
            ) : (
              <p className="text-center text-sec text-muted">Você chegou ao começo do histórico.</p>
            )}
          </>
        )}
      </Card>
    </>
  );
}
