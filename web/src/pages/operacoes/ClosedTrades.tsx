import { useQueryClient } from '@tanstack/react-query';
import { Download, Search } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { downloadTradesCsv, useTrades, useViewMode, type TradeFilters } from '@/api/marketQueries';
import type { Trade } from '@/api/marketSchemas';
import { KEYS, useSettings } from '@/api/queries';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { EmptyState } from '@/components/kit/EmptyState';
import { notify } from '@/components/kit/Toaster';
import { Button } from '@/components/ui/button';
import { Sheet } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { LabeledSelect } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { directionOf, formatNumber, formatSigned } from '@/lib/format';
import { cn } from '@/lib/utils';
import { RESULT_LOOK } from './look';
import { TradeDetail } from './TradeDetail';

type Period = 'all' | '7' | '30';
type ResultChoice = 'all' | 'loss' | 'breakeven';

const stamp = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso));

const sinceOf = (period: Period): string | undefined => {
  if (period === 'all') return undefined;
  const d = new Date();
  d.setUTCHours(0, 0, 0, 0);
  d.setUTCDate(d.getUTCDate() - Number(period));
  return d.toISOString();
};

const EXIT_OPTIONS = [
  ['stop_loss', 'Stop'],
  ['take_profit', 'Alvo'],
  ['profit_protection', 'Proteção do lucro'],
  ['signal', 'Sinal de saída'],
  ['manual', 'Manual'],
] as const;

function useDebounced<T>(value: T, ms: number) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

export function ClosedTrades() {
  const qc = useQueryClient();
  const mode = useViewMode();
  const settings = useSettings();
  const [search, setSearch] = useState('');
  const [symbol, setSymbol] = useState('');
  const [result, setResult] = useState<ResultChoice>('all');
  const [exit, setExit] = useState('');
  const [period, setPeriod] = useState<Period>('all');
  const [selected, setSelected] = useState<Trade | null>(null);
  const [exporting, setExporting] = useState(false);
  const q = useDebounced(search.trim(), 300);

  const filters = useMemo<TradeFilters>(
    () => ({
      ...(symbol ? { symbol } : {}),
      ...(result !== 'all' ? { result } : {}),
      ...(exit ? { exit_reason: exit } : {}),
      ...(period !== 'all' ? { since: sinceOf(period) } : {}),
      ...(q ? { q } : {}),
    }),
    [symbol, result, exit, period, q],
  );
  const filtered = Object.keys(filters).length > 0 || search !== '';
  const list = useTrades(mode, filters);
  const items = useMemo(() => {
    const seen = new Set<number>();
    return (list.data?.pages ?? [])
      .flatMap((p) => p.items)
      .filter((t) => !seen.has(t.id) && !!seen.add(t.id));
  }, [list.data]);
  const summary = list.data?.pages[0]?.summary;
  const symbols = settings.data?.document.pairs.symbols ?? [];

  const exportCsv = async () => {
    setExporting(true);
    try {
      const name = await downloadTradesCsv(mode, filters);
      notify.success('Exportação pronta', `Arquivo ${name} salvo.`);
    } catch {
      notify.error('Não foi possível exportar', 'Tente de novo em instantes.');
    } finally {
      setExporting(false);
    }
  };

  const clear = () => {
    setSearch('');
    setSymbol('');
    setResult('all');
    setExit('');
    setPeriod('all');
  };

  return (
    <div className="flex flex-col gap-4">
      <div
        className="flex flex-wrap items-end gap-3"
        role="group"
        aria-label="Filtros do histórico"
      >
        <label className="flex min-w-[200px] flex-1 flex-col gap-1 text-cap font-semibold text-muted">
          Buscar
          <span className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted"
              aria-hidden="true"
            />
            <Input
              type="search"
              value={search}
              maxLength={60}
              placeholder="Par, estratégia ou motivo"
              onChange={(e) => setSearch(e.target.value)}
              className="pl-9"
            />
          </span>
        </label>
        <LabeledSelect label="Par" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
          <option value="">Todos</option>
          {symbols.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </LabeledSelect>
        <LabeledSelect
          label="Resultado"
          value={result}
          onChange={(e) => setResult(e.target.value as ResultChoice)}
        >
          <option value="all">Todos</option>
          <option value="loss">Só perdas</option>
          <option value="breakeven">Só empates</option>
        </LabeledSelect>
        <LabeledSelect
          label="Motivo da saída"
          value={exit}
          onChange={(e) => setExit(e.target.value)}
        >
          <option value="">Todos</option>
          {EXIT_OPTIONS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </LabeledSelect>
        <LabeledSelect
          label="Período"
          value={period}
          onChange={(e) => setPeriod(e.target.value as Period)}
        >
          <option value="all">Tudo</option>
          <option value="7">Últimos 7 dias</option>
          <option value="30">Últimos 30 dias</option>
        </LabeledSelect>
        {filtered ? <Button onClick={clear}>Limpar filtros</Button> : null}
        <Button onClick={() => void exportCsv()} disabled={exporting || items.length === 0}>
          <Download className="size-4" aria-hidden="true" />
          {exporting ? 'Exportando…' : 'Exportar CSV'}
        </Button>
      </div>

      {summary ? (
        <p className="text-body tabular-nums" data-testid="resumo-operacoes">
          {filtered ? 'Com os filtros: ' : ''}
          {summary.count} {summary.count === 1 ? 'operação' : 'operações'} ·{' '}
          <span>{summary.wins} ganhos</span> ·{' '}
          <strong className="text-loss">▼ {summary.losses} perdas</strong> · resultado{' '}
          <strong
            className={cn(summary.pnl < 0 ? 'text-loss' : summary.pnl > 0 ? 'text-gain' : '')}
          >
            {formatSigned(summary.pnl)} USDT
          </strong>{' '}
          · taxas {formatNumber(summary.fees)} USDT
        </p>
      ) : null}

      {list.isError ? (
        <CardError
          error={list.error}
          onRetry={() => void qc.invalidateQueries({ queryKey: KEYS.trades })}
          retrying={list.isFetching}
          updatedAt={list.dataUpdatedAt || undefined}
        />
      ) : list.isPending ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <Skeleton key={i} className="h-11" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          title={filtered ? 'Nenhuma operação com este filtro' : 'Nenhuma operação encerrada ainda'}
        >
          {filtered
            ? 'Tente outros filtros ou limpe-os para ver tudo.'
            : 'O robô só opera quando surge uma boa oportunidade. Ficar parado em USDT é uma decisão válida.'}
        </EmptyState>
      ) : (
        <>
          <Sheet open={selected !== null} onOpenChange={(o) => !o && setSelected(null)}>
            <div
              tabIndex={0}
              role="region"
              aria-label="Operações encerradas"
              className="overflow-x-auto"
            >
              <table className="w-full min-w-[820px] border-collapse text-left">
                <thead>
                  <tr className="text-sec text-muted">
                    {[
                      'Encerrada',
                      'Par',
                      'Estratégia',
                      'Entrada → Saída',
                      'Motivo da saída',
                      'Resultado',
                      'R',
                      'USDT',
                      'Taxas',
                      '',
                    ].map((h, i) => (
                      <th
                        key={h || `acao-${i}`}
                        scope="col"
                        className="border-b border-border px-2 py-2 font-semibold"
                      >
                        {h || <span className="sr-only">Detalhes</span>}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {items.map((t) => {
                    const look = RESULT_LOOK[t.result];
                    const dir = directionOf(t.pnl);
                    return (
                      <tr
                        key={t.id}
                        data-result={t.result}
                        className={cn('tabular-nums', t.result === 'loss' && 'bg-loss-bg')}
                      >
                        <td className="px-2 py-2.5">{stamp(t.closed_at)}</td>
                        <th scope="row" className="px-2 py-2.5 font-semibold">
                          {t.symbol}
                        </th>
                        <td className="px-2 py-2.5">{t.strategy ?? '—'}</td>
                        <td className="px-2 py-2.5">
                          {formatNumber(t.entry_price)} → {formatNumber(t.exit_price)}
                        </td>
                        <td className="px-2 py-2.5">{t.exit_reason_text}</td>
                        <td className="px-2 py-2.5">
                          <Chip tone={look.tone} icon={look.icon}>
                            {look.label}
                          </Chip>
                        </td>
                        <td className="px-2 py-2.5">
                          {t.r_multiple === null ? '—' : formatSigned(t.r_multiple, 1)}
                        </td>
                        <td
                          className={cn(
                            'px-2 py-2.5 font-semibold',
                            dir === 'up'
                              ? 'text-gain'
                              : dir === 'down'
                                ? 'text-loss'
                                : 'text-muted',
                          )}
                        >
                          <span aria-hidden="true">
                            {dir === 'up' ? '▲ ' : dir === 'down' ? '▼ ' : ''}
                          </span>
                          {formatSigned(t.pnl)}
                        </td>
                        <td className="px-2 py-2.5">{formatNumber(t.fees)}</td>
                        <td className="px-2 py-2.5">
                          <Button
                            onClick={() => setSelected(t)}
                            aria-label={`Ver detalhes da operação ${t.symbol} de ${stamp(t.closed_at)}`}
                          >
                            Detalhes
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {selected ? <TradeDetail trade={selected} /> : null}
          </Sheet>
          {list.hasNextPage ? (
            <Button onClick={() => void list.fetchNextPage()} disabled={list.isFetchingNextPage}>
              {list.isFetchingNextPage ? 'Carregando…' : 'Carregar mais operações'}
            </Button>
          ) : (
            <p className="text-center text-sec text-muted">Fim do histórico.</p>
          )}
        </>
      )}
    </div>
  );
}
