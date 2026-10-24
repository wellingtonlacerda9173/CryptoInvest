import { Info, TriangleAlert } from 'lucide-react';
import { Tabs } from 'radix-ui';
import { useState, type ReactNode } from 'react';
import type { BacktestReport, Metrics, SymbolReport } from '@/api/backtestSchemas';
import { Chip } from '@/components/kit/Chip';
import { EquityChart } from '@/components/kit/EquityChart';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { KpiCard } from '@/components/kit/KpiCard';
import { ModeBadge } from '@/components/kit/ModeBadge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { formatNumber, formatPercent, formatSigned } from '@/lib/format';
import { cn } from '@/lib/utils';
import {
  asError,
  avoidedBlocks,
  codeLabel,
  flagText,
  KIND_LABEL,
  MIN_TRADES,
  verdictOf,
} from './reportModel';

const TH = 'border-b border-border px-2 py-1.5 font-semibold';
const dayMonth = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit', year: '2-digit' }).format(
    new Date(iso),
  );
const dash = (v: number | null, fmt: (n: number) => string) => (v === null ? '—' : fmt(v));
const pnlClass = (v: number) => (v < 0 ? 'text-loss' : 'text-text');
const pnlGlyph = (v: number) => (v < 0 ? '▼ ' : v > 0 ? '▲ ' : '');

function SampleWarning({ trades, what }: { trades: number; what: string }) {
  if (trades >= MIN_TRADES) return null;
  return (
    <div
      role="note"
      className="flex items-start gap-2 rounded-xl border border-warn bg-warn-bg px-3.5 py-2.5 text-sec"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
      <span>
        <strong>Amostra pequena.</strong> {trades} {trades === 1 ? 'operação' : 'operações'} {what}:
        poucas para concluir. Trate o resultado como inconclusivo.
      </span>
    </div>
  );
}

function Table({
  caption,
  head,
  children,
}: {
  caption: string;
  head: string[];
  children: ReactNode;
}) {
  return (
    <div tabIndex={0} role="region" aria-label={`${caption}: tabela`} className="overflow-x-auto">
      <table className="w-full min-w-[460px] border-collapse text-left text-sec tabular-nums">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="text-muted">
            {head.map((h) => (
              <th key={h} scope="col" className={TH}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

function MetricsGrid({ m }: { m: Metrics }) {
  return (
    <>
      <section
        aria-label="Risco e consistência"
        className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4"
      >
        <KpiCard
          label={<GlossaryTerm term="drawdown">Queda máxima (drawdown)</GlossaryTerm>}
          value={formatPercent(m.max_drawdown * 100, 1)}
          tone={m.max_drawdown >= 0.05 ? 'warn' : 'neutral'}
          glyph={false}
          delta={`pior queda, durou ${formatNumber(m.max_drawdown_days, 1)} dias`}
        />
        <KpiCard
          label="Meses no positivo"
          value={dash(m.positive_months_ratio, (v) => formatPercent(v * 100, 0))}
          glyph={false}
          delta={`${m.monthly.length} ${m.monthly.length === 1 ? 'mês' : 'meses'} no teste`}
        />
        <KpiCard
          label="Pior mês"
          value={dash(m.worst_month_return, (v) => formatPercent(v * 100, 1, true))}
          tone={(m.worst_month_return ?? 0) < 0 ? 'loss' : 'neutral'}
        />
        <KpiCard
          label="Tempo parado"
          value={formatPercent(m.idle_pct * 100, 0)}
          glyph={false}
          delta="sem posição (não é falha)"
        />
      </section>
      <section aria-label="Resultado" className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <KpiCard
          label="Retorno total"
          value={formatPercent(m.total_return * 100, 1, true)}
          tone={m.total_return < 0 ? 'loss' : 'info'}
          delta={`${formatSigned(m.total_pnl)} USDT`}
        />
        <KpiCard
          label={<GlossaryTerm term="profit-factor" />}
          value={dash(m.profit_factor, (v) => formatNumber(v))}
          glyph={false}
        />
        <KpiCard
          label={<GlossaryTerm term="taxa-de-acerto" />}
          value={dash(m.win_rate, (v) => formatPercent(v * 100, 0))}
          glyph={false}
          delta={`${m.wins} ganhos, ${m.losses} perdas`}
        />
        <KpiCard
          label={<GlossaryTerm term="expectativa">Expectativa (R)</GlossaryTerm>}
          value={dash(m.expectancy_r, (v) => formatSigned(v))}
          tone={(m.expectancy_r ?? 0) < 0 ? 'loss' : 'neutral'}
        />
        <KpiCard
          label={<GlossaryTerm term="payoff" />}
          value={dash(m.payoff, (v) => formatNumber(v))}
          glyph={false}
        />
        <KpiCard
          label={<GlossaryTerm term="sharpe" />}
          value={dash(m.sharpe, (v) => formatNumber(v))}
          glyph={false}
        />
        <KpiCard label="Operações" value={String(m.trades)} glyph={false} />
        <KpiCard label="Taxas pagas" value={formatNumber(m.fees_paid)} unit="USDT" glyph={false} />
      </section>
    </>
  );
}

function TradesCard({ r }: { r: SymbolReport }) {
  const [all, setAll] = useState(false);
  const rows = [...r.trades].reverse();
  const visible = all ? rows : rows.slice(0, 15);
  return (
    <Card aria-labelledby={`tr-${r.symbol}`}>
      <CardHeader>
        <CardTitle id={`tr-${r.symbol}`}>Operações simuladas</CardTitle>
      </CardHeader>
      {rows.length === 0 ? (
        <p className="text-sec text-muted">
          Nenhuma operação encerrada neste período. Ficar parado também é um resultado.
        </p>
      ) : (
        <>
          <Table
            caption={`Operações simuladas de ${r.symbol}`}
            head={[
              'Abertura',
              'Fechamento',
              'Estratégia',
              'Entrada',
              'Resultado (USDT)',
              'R',
              'Saída',
            ]}
          >
            {visible.map((t) => (
              <tr key={`${t.opened_at}-${t.strategy}`} className="border-b border-border">
                <td className="px-2 py-2">{dayMonth(t.opened_at)}</td>
                <td className="px-2 py-2">{dayMonth(t.closed_at)}</td>
                <td className="px-2 py-2">{t.strategy}</td>
                <td className="px-2 py-2">{formatNumber(t.entry_price)}</td>
                <td className={cn('px-2 py-2 font-semibold', pnlClass(t.pnl))}>
                  <span aria-hidden="true">{pnlGlyph(t.pnl)}</span>
                  {formatSigned(t.pnl)}
                  <span className="sr-only">
                    {t.pnl < 0 ? ' (perda)' : t.pnl > 0 ? ' (ganho)' : ''}
                  </span>
                </td>
                <td className="px-2 py-2">{formatSigned(t.r_multiple)}</td>
                <td className="px-2 py-2">{t.exits.join(', ') || '—'}</td>
              </tr>
            ))}
          </Table>
          <div className="flex flex-wrap items-center justify-between gap-2 text-sec text-muted">
            <span>
              Mostrando {visible.length} de {r.trades_total} operações
              {r.trades_total > r.trades.length
                ? ` (o relatório guarda as últimas ${r.trades.length})`
                : ''}
              .
            </span>
            {rows.length > 15 ? (
              <Button onClick={() => setAll((v) => !v)}>{all ? 'Ver menos' : 'Ver todas'}</Button>
            ) : null}
          </div>
        </>
      )}
    </Card>
  );
}

function AvoidedCard({ r }: { r: SymbolReport }) {
  const a = r.avoided;
  const blocks = avoidedBlocks(a);
  return (
    <Card aria-labelledby={`av-${r.symbol}`}>
      <CardHeader>
        <CardTitle id={`av-${r.symbol}`}>
          <GlossaryTerm term="operacoes-evitadas" />
        </CardTitle>
      </CardHeader>
      <p className="text-sec text-muted">
        {a.signals} sinais não viraram operação ({a.simulated} puderam ser simulados). Cada um foi
        simulado à parte: a soma <strong>não</strong> é o efeito real no saldo, e os três tipos
        abaixo não devem ser misturados.
      </p>
      <div className="grid gap-3 md:grid-cols-3">
        {blocks.map((b) => (
          <div key={b.kind} className="rounded-xl border border-border p-3.5">
            <strong>{b.title}</strong>
            <p className="text-sec text-muted">{b.explain}</p>
            <p className={cn('mt-1 text-[18px] font-bold tabular-nums', pnlClass(b.net))}>
              <span aria-hidden="true">{pnlGlyph(b.net)}</span>
              {formatSigned(b.net)} USDT
            </p>
            <p className="text-cap text-muted">
              {b.net > 0
                ? 'Os vetos pouparam esse valor.'
                : b.net < 0
                  ? 'Os vetos deixaram de ganhar esse valor.'
                  : 'Sem efeito medido.'}
            </p>
          </div>
        ))}
      </div>
      {a.by_code.length > 0 ? (
        <Table
          caption={`Operações evitadas de ${r.symbol}, por motivo`}
          head={['Motivo', 'Tipo', 'Sinais', 'Perdas evitadas', 'Ganhos perdidos']}
        >
          {a.by_code.map((g) => (
            <tr key={g.code} className="border-b border-border">
              <th scope="row" className="px-2 py-2 font-semibold">
                {codeLabel(g.code)}
              </th>
              <td className="px-2 py-2">{KIND_LABEL[g.kind] ?? g.kind}</td>
              <td className="px-2 py-2">{g.signals}</td>
              <td className="px-2 py-2">{formatNumber(g.avoided_losses)}</td>
              <td className="px-2 py-2">{formatNumber(g.forgone_gains)}</td>
            </tr>
          ))}
        </Table>
      ) : null}
      {a.halt_at ? (
        <p role="note" className="text-sec text-muted">
          O robô simulado se desligou em {dayMonth(a.halt_at)} (queda máxima atingida) e deixou de
          operar depois disso.
        </p>
      ) : null}
    </Card>
  );
}

function WalkForwardCard({ r }: { r: SymbolReport }) {
  const wf = r.walk_forward;
  const error = asError(wf);
  return (
    <Card aria-labelledby={`wf-${r.symbol}`}>
      <CardHeader>
        <CardTitle id={`wf-${r.symbol}`}>
          Conferência fora da amostra (<GlossaryTerm term="walk-forward" />)
        </CardTitle>
      </CardHeader>
      {wf === null ? (
        <p className="text-sec text-muted">
          Este teste rodou sem conferência em janelas: o resultado acima é só do passado conhecido e
          pode estar ajustado a ele. Refaça com o teste em janelas ligado para conferir.
        </p>
      ) : error !== null ? (
        <p role="note" className="text-sec text-muted">
          Não foi possível conferir em janelas: {error}
        </p>
      ) : 'assessment' in wf ? (
        (() => {
          const v = verdictOf(wf.assessment.verdict);
          const oos = wf.out_of_sample;
          return (
            <>
              <div
                data-testid="veredito"
                className="flex flex-col gap-2 rounded-xl border border-border-strong bg-surface-2 p-3.5"
              >
                <span className="flex flex-wrap items-center gap-2">
                  <Chip tone={v.tone}>{v.label}</Chip>
                  <GlossaryTerm term="sobreajuste">O que é sobreajuste?</GlossaryTerm>
                </span>
                <p>{v.text}</p>
                {wf.assessment.flags.length > 0 ? (
                  <ul className="list-disc pl-5 text-sec text-muted">
                    {wf.assessment.flags.map((f) => (
                      <li key={f}>{flagText(f)}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
              <SampleWarning trades={oos.trades} what="fora da amostra" />
              <section
                aria-label="Resultado fora da amostra"
                className="grid grid-cols-2 gap-3 lg:grid-cols-4"
              >
                <KpiCard
                  label="Queda máxima fora da amostra"
                  value={formatPercent(oos.max_drawdown * 100, 1)}
                  glyph={false}
                />
                <KpiCard
                  label="Retorno fora da amostra"
                  value={formatPercent(oos.total_return * 100, 1, true)}
                  tone={oos.total_return < 0 ? 'loss' : 'info'}
                />
                <KpiCard
                  label="Operações fora da amostra"
                  value={String(oos.trades)}
                  glyph={false}
                />
                <KpiCard
                  label="Expectativa (R)"
                  value={dash(oos.expectancy_r, (n) => formatSigned(n))}
                  glyph={false}
                />
              </section>
              <Table
                caption={`Janelas do teste de ${r.symbol}`}
                head={['Janela', 'Teste de', 'até', 'Parâmetros', 'Treino', 'Teste', 'Operações']}
              >
                {wf.windows.map((w) => (
                  <tr key={w.index} className="border-b border-border">
                    <th scope="row" className="px-2 py-2 font-semibold">
                      {w.index + 1}
                    </th>
                    <td className="px-2 py-2">{dayMonth(w.test_start)}</td>
                    <td className="px-2 py-2">{dayMonth(w.test_end)}</td>
                    <td className="px-2 py-2">
                      {w.selected
                        ? Object.entries(w.selected)
                            .map(([k, val]) => `${k}=${val}`)
                            .join(', ')
                        : 'nenhum'}
                    </td>
                    <td className="px-2 py-2">
                      {dash(w.train_return, (n) => formatPercent(n * 100, 1, true))}
                    </td>
                    <td className="px-2 py-2">
                      {dash(w.test_return, (n) => formatPercent(n * 100, 1, true))}
                    </td>
                    <td className="px-2 py-2">{w.test_trades ?? '—'}</td>
                  </tr>
                ))}
              </Table>
            </>
          );
        })()
      ) : null}
    </Card>
  );
}

function SymbolView({ r, capital }: { r: SymbolReport; capital: number }) {
  const points = r.equity_curve.map((p) => ({ at: p.at, equity: p.equity, realized: 0 }));
  return (
    <div className="flex flex-col gap-4">
      <SampleWarning trades={r.metrics.trades} what="neste teste" />
      <MetricsGrid m={r.metrics} />
      <Card aria-labelledby={`eq-${r.symbol}`}>
        <CardHeader>
          <CardTitle id={`eq-${r.symbol}`}>Curva de capital simulada</CardTitle>
        </CardHeader>
        {points.length > 1 ? (
          <EquityChart
            series={{ mode: 'backtest', simulated: true, initial_capital: capital, points }}
          />
        ) : (
          <p className="text-sec text-muted">Poucos pontos para desenhar a curva.</p>
        )}
      </Card>
      <WalkForwardCard r={r} />
      <AvoidedCard r={r} />
      {r.metrics.by_strategy.length > 0 ? (
        <Card aria-labelledby={`st-${r.symbol}`}>
          <CardHeader>
            <CardTitle id={`st-${r.symbol}`}>Por estratégia</CardTitle>
          </CardHeader>
          <Table
            caption={`Resultado por estratégia em ${r.symbol}`}
            head={['Estratégia', 'Operações', 'Acerto', 'Profit factor', 'Resultado (USDT)']}
          >
            {r.metrics.by_strategy.map((g) => (
              <tr key={g.name} className="border-b border-border">
                <th scope="row" className="px-2 py-2 font-semibold">
                  {g.name}
                </th>
                <td className="px-2 py-2">{g.trades}</td>
                <td className="px-2 py-2">{dash(g.win_rate, (n) => formatPercent(n * 100, 0))}</td>
                <td className="px-2 py-2">{dash(g.profit_factor, (n) => formatNumber(n))}</td>
                <td className={cn('px-2 py-2 font-semibold', pnlClass(g.pnl))}>
                  {formatSigned(g.pnl)}
                </td>
              </tr>
            ))}
          </Table>
        </Card>
      ) : null}
      <TradesCard r={r} />
    </div>
  );
}

function Assumptions({ report }: { report: BacktestReport }) {
  const a = report.assumptions;
  const pct = (v: string | number) => `${formatNumber(Number(v) * 100, 2)}%`;
  return (
    <Card aria-labelledby="prem-t">
      <CardHeader>
        <CardTitle id="prem-t">
          <Info className="size-5" aria-hidden="true" /> Premissas e limitações
        </CardTitle>
      </CardHeader>
      <ul className="flex list-disc flex-col gap-1 pl-5">
        <li>
          Período de {dayMonth(a.period.start)} a {dayMonth(a.period.end)}, velas de {a.timeframe},
          estratégias: {a.strategies.join(', ')}.
        </li>
        <li>
          Capital de {formatNumber(Number(a.capital), 0)} USDT, taxa de {pct(a.taker_fee)} (ordens a
          mercado) e {pct(a.maker_fee)} (limitadas), <GlossaryTerm term="deslizamento" /> de{' '}
          {formatNumber(Number(a.slippage_bps), 0)} pontos-base e <GlossaryTerm term="latencia" />{' '}
          de {a.latency_candles} vela(s).
        </li>
        {report.results.map((r) => (
          <li key={r.symbol}>
            {r.symbol}: {r.candles} velas, dados de {r.data_source}.
            {r.data_notes.length > 0 ? ` ${r.data_notes.join(' ')}` : ''}
          </li>
        ))}
        <li>
          A simulação usa velas fechadas e as premissas acima. Na vida real a execução pode diferir
          (fila de ordens, liquidez, falhas de conexão).
        </li>
        <li>
          O passado não garante o futuro: um bom resultado aqui não prova que a estratégia funciona.
        </li>
      </ul>
    </Card>
  );
}

export function Report({ report }: { report: BacktestReport }) {
  const capital = Number(report.assumptions.capital);
  const first = report.results[0];
  const multi = report.results.length > 1;
  return (
    <div className="flex flex-col gap-4">
      <div
        role="note"
        className="flex flex-wrap items-center gap-2.5 rounded-xl border border-dotted border-backtest bg-backtest-bg px-3.5 py-2.5"
      >
        <ModeBadge mode="backtest" />
        <span>{report.disclaimer}</span>
      </div>
      {multi ? (
        <Tabs.Root defaultValue={first?.symbol} className="flex flex-col gap-4">
          <Tabs.List aria-label="Par do relatório" className="flex border-b border-border">
            {report.results.map((r) => (
              <Tabs.Trigger
                key={r.symbol}
                value={r.symbol}
                className="min-h-11 cursor-pointer border-b-[3px] border-transparent bg-transparent px-4 font-semibold text-muted data-[state=active]:border-backtest data-[state=active]:text-text"
              >
                {r.symbol}
              </Tabs.Trigger>
            ))}
          </Tabs.List>
          {report.results.map((r) => (
            <Tabs.Content key={r.symbol} value={r.symbol}>
              <SymbolView r={r} capital={capital} />
            </Tabs.Content>
          ))}
        </Tabs.Root>
      ) : first ? (
        <SymbolView r={first} capital={capital} />
      ) : null}
      <Assumptions report={report} />
    </div>
  );
}
